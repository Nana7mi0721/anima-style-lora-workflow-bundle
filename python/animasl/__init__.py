"""animasl - Anima style LoRA dataset pipeline toolkit.

Stages: fetch -> import -> dedup -> screen -> text-remove -> rename -> wash -> config

The package is deliberately dependency-light (requests + pillow + numpy for the
core stages) so that it runs in any of the Python interpreters already present
on the machine.  The GPU stages (text detect / inpaint) additionally need
torch + rfdetr and are guarded by `animasl doctor`.
"""

__version__ = "0.1.0"
