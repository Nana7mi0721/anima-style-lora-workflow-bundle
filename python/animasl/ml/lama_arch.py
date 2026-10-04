"""LaMa FFC-ResNet generator (Big-LaMa / "anime-manga-big-lama" variant).

Provenance
----------
This is the LaMa generator from ``advimman/lama`` (Apache-2.0),
``saicinpainting/training/modules/{ffc,lama}.py``.  The module/parameter naming
below reproduces *exactly* the keys of the released TorchScript checkpoints
``big-lama.pt`` / ``anime-manga-big-lama.pt`` (the latter is what the
SafeTensors file ``mayocream/lama-manga`` was converted from), so the state dict
loads with ``strict=True``.

Semantics were cross-checked against the two upstream Python ports that consume
these very weights:

* ``manga-image-translator`` -- ``manga_translator/inpainting/inpainting_lama_mpe.py``
  (``LamaFourier`` / ``FFCResNetGenerator`` / ``FFC`` / ``FFC_BN_ACT`` /
  ``SpectralTransform`` / ``FourierUnit`` / ``FFCResnetBlock`` / ``ConcatTupleLayer``)
* ``IOPaint`` -- ``iopaint/model/lama.py``

Checkpoint layout that fixes the hyper-parameters (verified by dumping the
SafeTensors header):

* ``model.0``  param-less ``ReflectionPad2d(3)``
* ``model.1``  ``FFC_BN_ACT(4 -> 64, k=7, pad=0, ratio 0/0)``   (``convl2l`` is [64,4,7,7])
* ``model.2..4``  3 stride-2 ``FFC_BN_ACT`` -> 128 / 256 / 512; the last one has
  ``ratio_gout = 0.75`` (``bn_g`` = 384, ``bn_l`` = 128)
* ``model.5..22``  18 ``FFCResnetBlock(512, ratio 0.75/0.75)``  -> ``large_arch=True``
* ``model.23`` param-less ``ConcatTupleLayer``
* ``model.24..32`` 3 x (``ConvTranspose2d`` 512->256->128->64, ``BatchNorm2d``, ``ReLU``)
* ``model.33`` ``ReflectionPad2d(3)``, ``model.34`` ``Conv2d(64 -> 3, k=7, pad=0)``
* ``model.35`` ``Sigmoid`` (param-less; ``add_out_act='sigmoid'``), so the output
  lives in [0, 1] and the caller scales by 255.

``enable_lfu=False`` everywhere -- the checkpoint contains no ``*.lfu.*`` tensors.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

__all__ = ["FFCResNetGenerator", "build_lama_manga", "LAMA_MANGA_STATE_DICT_PREFIXES"]


# --------------------------------------------------------------------------------------
# Fourier / spectral blocks
# --------------------------------------------------------------------------------------
class FourierUnit(nn.Module):
    """1x1 conv + BN applied in the (real | imag)-stacked Fourier domain."""

    def __init__(self, in_channels, out_channels, groups=1, fft_norm="ortho"):
        super().__init__()
        self.groups = groups
        # real and imaginary parts are stacked as extra channels -> *2
        self.conv_layer = nn.Conv2d(
            in_channels=in_channels * 2,
            out_channels=out_channels * 2,
            kernel_size=1,
            stride=1,
            padding=0,
            groups=self.groups,
            bias=False,
        )
        self.bn = nn.BatchNorm2d(out_channels * 2)
        self.relu = nn.ReLU(inplace=True)
        self.fft_norm = fft_norm

    def forward(self, x):
        batch = x.shape[0]
        r_size = x.size()
        fft_dim = (-2, -1)
        ffted = torch.fft.rfftn(x, dim=fft_dim, norm=self.fft_norm)
        ffted = torch.stack((ffted.real, ffted.imag), dim=-1)
        ffted = ffted.permute(0, 1, 4, 2, 3).contiguous()  # (b, c, 2, h, w)
        ffted = ffted.view((batch, -1) + ffted.size()[3:])  # (b, c*2, h, w)

        ffted = self.conv_layer(ffted)
        ffted = self.relu(self.bn(ffted))

        ffted = ffted.view((batch, -1, 2) + ffted.size()[2:]).permute(
            0, 1, 3, 4, 2
        ).contiguous()  # (b, c, h, w, 2)
        ffted = torch.view_as_complex(ffted)
        output = torch.fft.irfftn(ffted, s=r_size[2:], dim=fft_dim, norm=self.fft_norm)
        return output


class SpectralTransform(nn.Module):
    """Global branch of an FFC layer: half-width 1x1 conv -> FourierUnit -> 1x1 conv."""

    def __init__(self, in_channels, out_channels, stride=1, groups=1, enable_lfu=False, **fu_kwargs):
        super().__init__()
        self.enable_lfu = enable_lfu
        if stride == 2:
            self.downsample = nn.AvgPool2d(kernel_size=(2, 2), stride=2)
        else:
            self.downsample = nn.Identity()
        self.stride = stride

        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels // 2, kernel_size=1, groups=groups, bias=False),
            nn.BatchNorm2d(out_channels // 2),
            nn.ReLU(inplace=True),
        )
        self.fu = FourierUnit(out_channels // 2, out_channels // 2, groups, **fu_kwargs)
        if self.enable_lfu:
            self.lfu = FourierUnit(out_channels // 2, out_channels // 2, groups)
        self.conv2 = nn.Conv2d(out_channels // 2, out_channels, kernel_size=1, groups=groups, bias=False)

    def forward(self, x):
        x = self.downsample(x)
        x = self.conv1(x)
        output = self.fu(x)

        if self.enable_lfu:
            n, c, h, w = x.shape
            split_no = 2
            split_s = h // split_no
            xs = torch.cat(torch.split(x[:, : c // 4], split_s, dim=-2), dim=1).contiguous()
            xs = torch.cat(torch.split(xs, split_s, dim=-1), dim=1).contiguous()
            xs = self.lfu(xs)
            xs = xs.repeat(1, 1, split_no, split_no).contiguous()
        else:
            xs = 0

        return self.conv2(x + output + xs)


# --------------------------------------------------------------------------------------
# FFC layer
# --------------------------------------------------------------------------------------
class FFC(nn.Module):
    """Fast Fourier Convolution: a local (1-ratio) and a global (ratio) branch."""

    def __init__(self, in_channels, out_channels, kernel_size, ratio_gin, ratio_gout, stride=1,
                 padding=0, dilation=1, groups=1, bias=False, enable_lfu=True,
                 padding_type="reflect", gated=False, **spectral_kwargs):
        super().__init__()
        assert stride == 1 or stride == 2, "Stride should be 1 or 2."
        self.stride = stride

        in_cg = int(in_channels * ratio_gin)
        in_cl = in_channels - in_cg
        out_cg = int(out_channels * ratio_gout)
        out_cl = out_channels - out_cg

        self.ratio_gin = ratio_gin
        self.ratio_gout = ratio_gout
        self.global_in_num = in_cg

        module = nn.Identity if in_cl == 0 or out_cl == 0 else nn.Conv2d
        self.convl2l = module(in_cl, out_cl, kernel_size, stride, padding, dilation, groups, bias,
                              padding_mode=padding_type)
        module = nn.Identity if in_cl == 0 or out_cg == 0 else nn.Conv2d
        self.convl2g = module(in_cl, out_cg, kernel_size, stride, padding, dilation, groups, bias,
                              padding_mode=padding_type)
        module = nn.Identity if in_cg == 0 or out_cl == 0 else nn.Conv2d
        self.convg2l = module(in_cg, out_cl, kernel_size, stride, padding, dilation, groups, bias,
                              padding_mode=padding_type)
        module = nn.Identity if in_cg == 0 or out_cg == 0 else SpectralTransform
        self.convg2g = module(in_cg, out_cg, stride, 1 if groups == 1 else groups // 2,
                              enable_lfu, **spectral_kwargs)

        self.gated = gated
        # upstream: the gate only exists when gated=True, otherwise it is a plain Identity
        module = nn.Identity if in_cg == 0 or out_cl == 0 or not self.gated else nn.Conv2d
        self.gate = module(in_channels, 2, 1)

    def forward(self, x):
        x_l, x_g = x if type(x) is tuple else (x, 0)
        out_xl, out_xg = 0, 0

        if self.gated:
            total_input_parts = [x_l]
            if torch.is_tensor(x_g):
                total_input_parts.append(x_g)
            total_input = torch.cat(total_input_parts, dim=1)
            gates = torch.sigmoid(self.gate(total_input))
            g2l_gate, l2g_gate = gates.chunk(2, dim=1)
        else:
            g2l_gate, l2g_gate = 1, 1

        if self.ratio_gout != 1:
            out_xl = self.convl2l(x_l) + self.convg2l(x_g) * g2l_gate
        if self.ratio_gout != 0:
            out_xg = self.convl2g(x_l) * l2g_gate + self.convg2g(x_g)

        return out_xl, out_xg


class FFC_BN_ACT(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, ratio_gin, ratio_gout, stride=1,
                 padding=0, dilation=1, groups=1, bias=False, norm_layer=nn.BatchNorm2d,
                 activation_layer=nn.Identity, padding_type="reflect", enable_lfu=True, **kwargs):
        super().__init__()
        self.ffc = FFC(in_channels, out_channels, kernel_size, ratio_gin, ratio_gout, stride,
                       padding, dilation, groups, bias, enable_lfu, padding_type=padding_type, **kwargs)
        lnorm = nn.Identity if ratio_gout == 1 else norm_layer
        gnorm = nn.Identity if ratio_gout == 0 else norm_layer
        global_channels = int(out_channels * ratio_gout)
        self.bn_l = lnorm(out_channels - global_channels)
        self.bn_g = gnorm(global_channels)

        lact = nn.Identity if ratio_gout == 1 else activation_layer
        gact = nn.Identity if ratio_gout == 0 else activation_layer
        self.act_l = lact(inplace=True)
        self.act_g = gact(inplace=True)

    def forward(self, x):
        x_l, x_g = self.ffc(x)
        x_l = self.act_l(self.bn_l(x_l))
        x_g = self.act_g(self.bn_g(x_g))
        return x_l, x_g


class FFCResnetBlock(nn.Module):
    def __init__(self, dim, padding_type, norm_layer, activation_layer=nn.ReLU, dilation=1,
                 spatial_transform_kwargs=None, inline=False, **conv_kwargs):
        super().__init__()
        self.conv1 = FFC_BN_ACT(dim, dim, kernel_size=3, padding=dilation, dilation=dilation,
                                norm_layer=norm_layer, activation_layer=activation_layer,
                                padding_type=padding_type, **conv_kwargs)
        self.conv2 = FFC_BN_ACT(dim, dim, kernel_size=3, padding=dilation, dilation=dilation,
                                norm_layer=norm_layer, activation_layer=activation_layer,
                                padding_type=padding_type, **conv_kwargs)
        self.inline = inline

    def forward(self, x):
        if self.inline:
            x_l, x_g = x[:, : -self.conv1.ffc.global_in_num], x[:, -self.conv1.ffc.global_in_num:]
        else:
            x_l, x_g = x if type(x) is tuple else (x, 0)

        id_l, id_g = x_l, x_g

        x_l, x_g = self.conv1((x_l, x_g))
        x_l, x_g = self.conv2((x_l, x_g))
        x_l, x_g = id_l + x_l, id_g + x_g

        out = x_l, x_g
        if self.inline:
            out = torch.cat(out, dim=1)
        return out


class ConcatTupleLayer(nn.Module):
    def forward(self, x):
        assert isinstance(x, tuple)
        x_l, x_g = x
        assert torch.is_tensor(x_l) or torch.is_tensor(x_g)
        if not torch.is_tensor(x_g):
            return x_l
        return torch.cat(x, dim=1)


def get_activation(kind="tanh"):
    if kind == "tanh":
        return nn.Tanh()
    if kind == "sigmoid":
        return nn.Sigmoid()
    if kind is False:
        return nn.Identity()
    raise ValueError(f"Unknown activation kind {kind}")


# --------------------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------------------
class FFCResNetGenerator(nn.Module):
    """Big-LaMa generator.  ``self.model`` is an ``nn.Sequential`` whose *index* in the
    list is what the checkpoint keys (``model.<idx>...``) refer to, so the list must be
    built in exactly the same order as upstream."""

    def __init__(self, input_nc=4, output_nc=3, ngf=64, n_downsampling=3, n_blocks=18,
                 norm_layer=nn.BatchNorm2d, padding_type="reflect", activation_layer=nn.ReLU,
                 up_norm_layer=nn.BatchNorm2d, up_activation=nn.ReLU(True),
                 init_conv_kwargs=None, downsample_conv_kwargs=None, resnet_conv_kwargs=None,
                 spatial_transform_kwargs=None, add_out_act="sigmoid", max_features=1024,
                 out_ffc=False, out_ffc_kwargs=None):
        assert n_blocks >= 0
        super().__init__()
        init_conv_kwargs = dict(init_conv_kwargs or {})
        downsample_conv_kwargs = dict(downsample_conv_kwargs or {})
        resnet_conv_kwargs = dict(resnet_conv_kwargs or {})
        spatial_transform_kwargs = dict(spatial_transform_kwargs or {})
        out_ffc_kwargs = dict(out_ffc_kwargs or {})

        model = [nn.ReflectionPad2d(3),
                 FFC_BN_ACT(input_nc, ngf, kernel_size=7, padding=0, norm_layer=norm_layer,
                            activation_layer=activation_layer, **init_conv_kwargs)]

        # downsample
        for i in range(n_downsampling):
            mult = 2 ** i
            if i == n_downsampling - 1:
                cur_conv_kwargs = dict(downsample_conv_kwargs)
                cur_conv_kwargs["ratio_gout"] = resnet_conv_kwargs.get("ratio_gin", 0)
            else:
                cur_conv_kwargs = downsample_conv_kwargs
            model += [FFC_BN_ACT(min(max_features, ngf * mult),
                                 min(max_features, ngf * mult * 2),
                                 kernel_size=3, stride=2, padding=1,
                                 norm_layer=norm_layer,
                                 activation_layer=activation_layer,
                                 **cur_conv_kwargs)]

        mult = 2 ** n_downsampling
        feats_num_bottleneck = min(max_features, ngf * mult)

        for _ in range(n_blocks):
            model += [FFCResnetBlock(feats_num_bottleneck, padding_type=padding_type,
                                     activation_layer=activation_layer, norm_layer=norm_layer,
                                     **resnet_conv_kwargs)]

        model += [ConcatTupleLayer()]

        # upsample
        for i in range(n_downsampling):
            mult = 2 ** (n_downsampling - i)
            model += [nn.ConvTranspose2d(min(max_features, ngf * mult),
                                         min(max_features, int(ngf * mult / 2)),
                                         kernel_size=3, stride=2, padding=1, output_padding=1),
                      up_norm_layer(min(max_features, int(ngf * mult / 2))),
                      up_activation]

        if out_ffc:
            model += [FFCResnetBlock(ngf, padding_type=padding_type,
                                     activation_layer=activation_layer, norm_layer=norm_layer,
                                     inline=True, **out_ffc_kwargs)]

        model += [nn.ReflectionPad2d(3),
                  nn.Conv2d(ngf, output_nc, kernel_size=7, padding=0)]
        if add_out_act:
            model.append(get_activation("tanh" if add_out_act is True else add_out_act))
        self.model = nn.Sequential(*model)

    def forward(self, img, mask=None, rel_pos=None, direct=None):
        """``img``: (B,3,H,W) in [0,1]; ``mask``: (B,1,H,W) with 1 = hole."""
        if mask is None:
            # single-tensor entry point (already 4-channel, RGB*(1-m) | m)
            if rel_pos is None:
                return self.model(img)
            raise ValueError("rel_pos requires img + mask")
        masked_img = torch.cat([img * (1 - mask), mask], dim=1)
        if rel_pos is None:
            return self.model(masked_img)
        x_l, x_g = self.model[:2](masked_img)
        x_l = x_l.to(torch.float32)
        x_l += rel_pos
        x_l += direct
        return self.model[2:]((x_l, x_g))


# --------------------------------------------------------------------------------------
# Factory matching `anime-manga-big-lama.pt` / `lama-manga.safetensors`
# --------------------------------------------------------------------------------------
def build_lama_manga(n_blocks: int = 18, add_out_act="sigmoid") -> FFCResNetGenerator:
    """Exactly the configuration used to produce ``anime-manga-big-lama.pt``
    (``LamaFourier(large_arch=True)`` in manga-image-translator)."""
    return FFCResNetGenerator(
        4, 3,
        add_out_act=add_out_act,
        n_blocks=n_blocks,
        init_conv_kwargs={"ratio_gin": 0, "ratio_gout": 0, "enable_lfu": False},
        downsample_conv_kwargs={"ratio_gin": 0, "ratio_gout": 0, "enable_lfu": False},
        resnet_conv_kwargs={"ratio_gin": 0.75, "ratio_gout": 0.75, "enable_lfu": False},
    )


LAMA_MANGA_STATE_DICT_PREFIXES = ("model.",)
