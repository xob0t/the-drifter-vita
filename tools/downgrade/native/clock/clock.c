/* DrifterClock: a PS Vita native plugin (PRX) that sets the ARM and GPU clocks. The 2018.2 player's start
   applies the project's power mode (scePowerSetConfigurationMode, mode C), which puts the CPU at 333 MHz;
   compat/Clock.cs calls these through [DllImport("DrifterClock")] to raise it again. The SDK's
   libScePower_stub has no clock functions; build.py makes a stub library for their NIDs. */
#include <kernel.h>
#include <moduleinfo.h>

SCE_MODULE_INFO(DrifterClock, SCE_MODULE_ATTR_NONE, 1, 1);

int scePowerSetArmClockFrequency(int mhz);
int scePowerGetArmClockFrequency(void);
int scePowerSetGpuClockFrequency(int mhz);
int scePowerGetGpuClockFrequency(void);

#define PRX_EXPORT __declspec(dllexport)

int module_start(SceSize size, const void *arg)
{
	(void)size;
	(void)arg;
	return SCE_KERNEL_START_SUCCESS;
}

int module_stop(SceSize size, const void *arg)
{
	(void)size;
	(void)arg;
	return SCE_KERNEL_STOP_SUCCESS;
}

/* Returns the system's result (0 or an SCE error code). */
PRX_EXPORT int DrifterClockSetArm(int mhz)
{
	return scePowerSetArmClockFrequency(mhz);
}

/* The ARM clock in MHz. */
PRX_EXPORT int DrifterClockGetArm(void)
{
	return scePowerGetArmClockFrequency();
}

/* The GPU core clock: 41, 55, 111, 166 or 222 MHz. Returns the system's result. */
PRX_EXPORT int DrifterClockSetGpu(int mhz)
{
	return scePowerSetGpuClockFrequency(mhz);
}

/* The GPU core clock in MHz. */
PRX_EXPORT int DrifterClockGetGpu(void)
{
	return scePowerGetGpuClockFrequency();
}
