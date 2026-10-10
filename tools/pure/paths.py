"""Paths the Unity-free build reads and writes."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools' / 'downgrade'))
import common  # noqa: E402

TARGET = common.TARGET
# The translated project the pipeline's rules produce (pipeline.py project --platform vita).
PROJECT = common.WORK / f'drifter-{TARGET}-vita'
# The player Unity built from that project: the reference the checks compare against.
REFERENCE = ROOT / 'builds' / f'drifter-{TARGET}-vita'
REFERENCE_MEDIA = REFERENCE / 'Media'
# Files taken from Unity's installers (CGIncludes, the PS Vita player and its shader includes).
EDITOR_DATA = common.EDITORS / TARGET / 'Editor' / 'Data'
CGINCLUDES = EDITOR_DATA / 'CGIncludes'
PSP2_PLAYER = EDITOR_DATA / 'PlaybackEngines' / 'PSP2Player'
SDK = Path(common.setting('SCE_PSP2_SDK_DIR') or r'C:\vitasdk\sdk')
PSP2CGC = SDK / 'host_tools' / 'bin' / 'psp2cgc.exe'
# Scratch space and outputs.
WORK = common.WORK / 'pure'
OUT = ROOT / 'builds' / f'drifter-{TARGET}-vita-pure'
