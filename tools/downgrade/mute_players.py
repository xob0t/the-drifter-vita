"""Keep every running game player muted in the Windows mixer, whoever launched it.

    uv run --with pycaw tools/downgrade/mute_players.py

Test runs launch the original and translated PC players from many tools (diag.py, sweep.py,
crt_compare.py, IL2CPP diag builds with an older probe compiled in). The probe mutes the
players it is injected into; this catches the rest by muting the Windows audio session of
every TheDrifter.exe, checking twice a second, until stopped.
"""

import time

from pycaw.pycaw import AudioUtilities


def mute():
    muted = 0
    for session in AudioUtilities.GetAllSessions():
        process = session.Process
        if process is None or process.name().lower() != 'thedrifter.exe':
            continue
        volume = session.SimpleAudioVolume
        if not volume.GetMute():
            volume.SetMute(1, None)
            muted += 1
    return muted


def main():
    while True:
        try:
            count = mute()
            if count:
                print(f'{time.strftime("%H:%M:%S")} muted {count} session(s)', flush=True)
        except Exception as error:  # a process can exit between listing and muting
            print(f'{time.strftime("%H:%M:%S")} {error!r}', flush=True)
        time.sleep(0.5)


if __name__ == '__main__':
    main()
