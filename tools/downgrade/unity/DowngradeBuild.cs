using System;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

// Batch-mode entry point for the downgrade pipeline. Builds the scenes listed in
// the recovered EditorBuildSettings.
//   -buildOutput <path>           player executable (Windows) or folder (Vita)
//   -buildTarget Win64|PSP2       Unity's flag; PSP2 needs Unity's PS Vita build module and Sony's SDK
//   -scriptingBackend mono|il2cpp Windows only; the Vita target is IL2CPP only
public static class DowngradeBuild
{
    // Homebrew title: XXYYYY-<9 char title ID>_00-<16 char label>.
    const string VitaContentId = "IV0000-DRFT00001_00-THEDRIFTERVITA00";

    public static void Build()
    {
        string[] args = Environment.GetCommandLineArgs();
        string output = Argument(args, "-buildOutput");
        if (output == null) throw new ArgumentException("Missing -buildOutput <path>");
        string requested = Argument(args, "-buildTarget"); // Unity's own flag; it also selects the import target
        if (requested != "PSP2" && requested != "Win64") throw new ArgumentException("Unsupported -buildTarget " + requested);
        bool vita = requested == "PSP2";
        bool il2cpp = vita || Argument(args, "-scriptingBackend") == "il2cpp";
        string[] scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();

        BuildTarget target = BuildTarget.StandaloneWindows64;
        BuildTargetGroup group = BuildTargetGroup.Standalone;
        if (vita)
        {
            target = BuildTarget.PSP2;
            group = BuildTargetGroup.PSP2;
            if (!BuildPipeline.IsBuildTargetSupported(group, target) ||
                !EditorUserBuildSettings.SwitchActiveBuildTarget(group, target))
            {
                Debug.LogError("DOWNGRADE_BUILD Failed: PS Vita build support is not installed for this editor");
                EditorApplication.Exit(2);
                return;
            }
            EditorUserBuildSettings.psp2BuildSubtarget = PSP2BuildSubtarget.PCHosted;
            PlayerSettings.PSVita.contentID = VitaContentId;
            PlayerSettings.PSVita.shortTitle = "The Drifter";
            // The Vita module calls IL2CPP without null and array bounds checks, so a null
            // reference or bad index the PC game survives as a logged exception crashes the
            // player instead. Keep the exception behaviour of the source version.
            PlayerSettings.SetAdditionalIl2CppArgs("--emit-null-checks --enable-array-bounds-check");
            // The largest application memory budget; the game's art is sized for PCs.
            PlayerSettings.PSVita.memoryExpansionMode = PlayerSettings.PSVita.PSVitaMemoryExpansionMode.ExpandBy109MB;
        }
        PlayerSettings.SetScriptingBackend(group, il2cpp ? ScriptingImplementation.IL2CPP : ScriptingImplementation.Mono2x);
        // The source game shows no Unity splash screen, and the project keeps that setting, but
        // the editor turns the splash back on when it loads a project under a license that
        // cannot hide it. Set it again, and log what the editor kept.
        PlayerSettings.SplashScreen.show = false;
        PlayerSettings.SplashScreen.showUnityLogo = false;
        Debug.Log("DOWNGRADE_SPLASH hasPro=" + UnityEditorInternal.InternalEditorUtility.HasPro() +
                  " show=" + PlayerSettings.SplashScreen.show + " showUnityLogo=" + PlayerSettings.SplashScreen.showUnityLogo);

        // pipeline.py build --development: the development player, for the engine's profiler markers.
        BuildOptions options = args.Contains("-developmentBuild") ? BuildOptions.Development : BuildOptions.None;
        BuildReport report = BuildPipeline.BuildPlayer(scenes, output, target, options);
        Debug.Log("DOWNGRADE_BUILD " + report.summary.result + " errors=" + report.summary.totalErrors +
                  " development=" + (options == BuildOptions.Development) +
                  " target=" + target + " backend=" + (il2cpp ? "il2cpp" : "mono") +
                  " scenes=" + scenes.Length + " size=" + report.summary.totalSize);
        // Build report messages do not all reach the editor log; keep them next to it.
        var messages = report.steps.SelectMany(s => s.messages.Select(m => m.type + "\t" + s.name + "\t" + m.content))
            .Where(m => !m.StartsWith("Log\t"));
        System.IO.File.WriteAllLines(output.TrimEnd('/', System.IO.Path.DirectorySeparatorChar) + ".messages.txt", messages.ToArray());
        EditorApplication.Exit(report.summary.result == BuildResult.Succeeded ? 0 : 1);
    }

    static string Argument(string[] args, string name)
    {
        int index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }
}
