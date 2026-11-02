using System;
using System.Runtime.InteropServices;
using UnityEngine;

// Clocks: the PS Vita's CPU and GPU clocks.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // The player's start applies the project's power mode (C, asset_rules.py PLATFORM_SETTING_RULES),
    // which sets the ARM cores to 333 MHz and the GPU to 166, whatever was set before. At 333 MHz
    // FMOD's mixer can fall behind and the sound crackles (docs/vita.md, "Clocks"). The DrifterClock
    // plugin (Media/Plugins/DrifterClock.suprx, tools/downgrade/native/clock) calls the system's
    // scePowerSet*ClockFrequency, which the SDK's stubs lack. Every 2 s this raises a clock that is
    // below its target again: the power mode puts the GPU back to 166 MHz about 13 s after the start,
    // and a suspend resets both. Only raised, so a higher clock (PSVshell's 500) stays; a manual
    // PSVshell setting overrides what the game sets.
    //   -compat-cpu-mhz <n>  the ARM clock to keep (default 444, the most an application may set;
    //                        0 leaves the power mode's 333)
    //   -compat-gpu-mhz <n>  the GPU core clock to keep, 111, 166 or 222 (default 222; 0 leaves it)
    // Main thread only.
    public static class Clock
    {
        const float Interval = 2f;
        const int CpuMhz = 444, GpuMhz = 222;
        static int cpuTarget, gpuTarget;
        static string[] configuredFor;
        static float next;
        static bool missing;

        [DllImport("DrifterClock")] static extern int DrifterClockSetArm(int mhz);
        [DllImport("DrifterClock")] static extern int DrifterClockGetArm();
        [DllImport("DrifterClock")] static extern int DrifterClockSetGpu(int mhz);
        [DllImport("DrifterClock")] static extern int DrifterClockGetGpu();

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            if (Application.platform != RuntimePlatform.PSP2) return;
            try { Debug.Log("Clock: CPU " + DrifterClockGetArm() + " MHz, GPU " + DrifterClockGetGpu() + " MHz at start"); }
            catch (Exception e)
            {
                Debug.Log("Clock: no DrifterClock plugin (" + e.GetType().Name + ")");
                return;
            }
            // The first poll is in the first frame, after the probe (a diag build's) has handed over
            // its flags in the first scene's LoadingScreen.OnEnable.
            UpdateHost.Create("Clock", Poll);
        }

        static void Poll()
        {
            float now = Time.realtimeSinceStartup;
            if (missing || now < next) return;
            next = now + Interval;
            // The flags are read again whenever the probe hands over its own (Platform.NewArgs).
            if (Platform.NewArgs(ref configuredFor))
            {
                cpuTarget = Mhz("-compat-cpu-mhz", CpuMhz);
                gpuTarget = Mhz("-compat-gpu-mhz", GpuMhz);
            }
            try
            {
                cpuTarget = Raise("CPU", false, cpuTarget);
                gpuTarget = Raise("GPU", true, gpuTarget);
            }
            catch (Exception e)
            {
                missing = true;
                Debug.Log("Clock: " + e.GetType().Name + ": " + e.Message);
            }
        }

        static int Mhz(string flag, int fallback)
        {
            int mhz;
            return int.TryParse(Platform.ArgValue(flag), out mhz) ? mhz : fallback;
        }

        // Sets a clock that is below the target and logs the change; returns the target to keep, 0 to
        // stop (a clock the system doesn't take would be set and logged every 2 s).
        static int Raise(string name, bool gpu, int target)
        {
            if (target <= 0) return 0;
            int before = gpu ? DrifterClockGetGpu() : DrifterClockGetArm();
            if (before >= target) return target;
            int result = gpu ? DrifterClockSetGpu(target) : DrifterClockSetArm(target);
            int after = gpu ? DrifterClockGetGpu() : DrifterClockGetArm();
            Debug.Log("Clock: " + name + " " + before + " -> " + after + " MHz" + (result != 0 ? " (error 0x" + result.ToString("X8") + ")" : ""));
            return after >= target ? target : after > before ? after : 0;
        }
    }
}
