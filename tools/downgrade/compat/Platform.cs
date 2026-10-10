using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// Platform answers and the PS Vita controls, CRT toggle and options entry, power ticks and memory setup.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // The game keeps saves and settings in "./", the working directory. That is the
    // install folder on desktop players but read-only app data on consoles such as
    // PS Vita. The downgrader replaces the "./" literal with this call.
    public static class Platform
    {
        public static string CurrentDirectory()
        {
            switch (Application.platform)
            {
                case RuntimePlatform.WindowsPlayer:
                case RuntimePlatform.OSXPlayer:
                case RuntimePlatform.LinuxPlayer:
                case RuntimePlatform.WindowsEditor:
                case RuntimePlatform.OSXEditor:
                case RuntimePlatform.LinuxEditor:
                    return "./";
                default:
                    // The PS Vita player has no persistentDataPath; keep saves in the data
                    // folder homebrew uses.
                    string folder = string.IsNullOrEmpty(Application.persistentDataPath)
                        ? "ux0:data/" + Application.productName.Replace(" ", "") : Application.persistentDataPath;
                    System.IO.Directory.CreateDirectory(folder);
                    return folder + "/";
            }
        }

        // The game turns its CRT filter off on screens under 540 lines (otherwise a saved setting
        // with only a debug key). On the PS Vita's 544 lines it stays off by default for now: even
        // drawn in two steps (see Crt) it takes about 7.5 ms of GPU time a frame (42-50 fps
        // under vsync instead of 60). Once the player has chosen a mode with Select + R or the
        // options entry (CrtToggle), or with the probe's -diag-crt on|off or -compat-crt-mode,
        // the game's limit applies and the saved setting decides.
        public static float LowResolutionLines()
        {
            return Application.platform == RuntimePlatform.PSP2 && !HasArg("-diag-crt") && !CrtToggle.Chosen && !CrtToggle.Forced ? 545f : 540f;
        }

        // The game's vsync and frame-limit options (QuestSettings.Vsync and TargetFrameRate, set
        // from its options menu and saved; LoadingScreen also sets 60) are PC options. The PS
        // Vita's display runs at 60 Hz: there frames are always synchronized to it, whatever the
        // saved options say. (A saved "vsync off" left the Vita running unsynchronized at
        // 85 fps, tearing.) The downgrader sends every QualitySettings.vSyncCount and
        // Application.targetFrameRate assignment in the game here.
        public static void SetVSyncCount(int count)
        {
            QualitySettings.vSyncCount = Application.platform == RuntimePlatform.PSP2 ? 1 : count;
        }

        public static void SetTargetFrameRate(int rate)
        {
            Application.targetFrameRate = Application.platform == RuntimePlatform.PSP2 ? 60 : rate;
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void SynchronizeVita()
        {
            if (Application.platform != RuntimePlatform.PSP2) return;
            SetVSyncCount(1);
            SetTargetFrameRate(60);
        }

        // Application.isFocused, read by the game where it waits for a voice line, a video or the
        // demo's end (PowerQuest.ShouldContinueDialog: a line advances once its voice has ended
        // and the window has focus). On the PS Vita the player never reports focus, so voiced
        // lines waited for a button press; an app runs there only in the foreground.
        public static bool IsFocused()
        {
            return Application.platform == RuntimePlatform.PSP2 || Application.isFocused;
        }

        // GlobalScript.PlatformSwitch: the game's console layout of its menus (VitaOptions).
        public static bool ConsoleUi(bool platformSwitch)
        {
            return platformSwitch || Application.platform == RuntimePlatform.PSP2;
        }

        // Flags the parity probe (diag/Diag.cs) read: on the PS Vita they come from a file
        // (ux0:data/drifter/args.txt), not the command line. The probe sets this at start.
        public static string[] ProbeArgs = new string[0];

        // The command line, read once: Environment.GetCommandLineArgs makes a new array on every
        // call, and LowResolutionLines asks HasArg every frame. (Any thread may fill it; every
        // copy is the same.)
        static string[] commandLine;

        static string[] CommandLine
        {
            get
            {
                string[] args = commandLine;
                if (args == null) commandLine = args = Environment.GetCommandLineArgs();
                return args;
            }
        }

        // Whether the command line or the probe's flags have this flag.
        public static bool HasArg(string name)
        {
            return Array.IndexOf(CommandLine, name) >= 0 || Array.IndexOf(ProbeArgs, name) >= 0;
        }

        // Whether the command line alone has this flag: switches read before the probe can hand
        // its flags over (-skip-empty-particle-cameras) or kept off them (-compat-stored-saves).
        internal static bool OnCommandLine(string name)
        {
            return Array.IndexOf(CommandLine, name) >= 0;
        }

        // Whether the flags changed since `seen` was set here (which it then is): a rule that keeps
        // what it read from them reads them again, as the probe hands its own over after it starts
        // (on the PS Vita the only way to pass them). True on the first call.
        internal static bool NewArgs(ref string[] seen)
        {
            if (ReferenceEquals(seen, ProbeArgs)) return false;
            seen = ProbeArgs;
            return true;
        }

        // A rule on by default on the PS Vita, where `off` turns it off; elsewhere on only with `on`.
        internal static bool VitaDefault(string off, string on)
        {
            return Application.platform == RuntimePlatform.PSP2 ? !HasArg(off) : HasArg(on);
        }

        // Types looked up by assembly-qualified name, each once (null when absent: a type of the
        // game's or the PS Vita player's is either in the build or not). Main thread only.
        internal const string PowerQuestType = "PowerTools.Quest.PowerQuest, Recovered.Drifter";
        internal const string SystemTextType = "PowerTools.Quest.SystemText, Recovered.Drifter";
        internal const string VitaUtilityType = "UnityEngine.PSVita.Utility, UnityEngine.PSVitaModule";
        static readonly Dictionary<string, Type> types = new Dictionary<string, Type>();

        internal static Type FindType(string name)
        {
            Type type;
            if (!types.TryGetValue(name, out type)) types[name] = type = Type.GetType(name);
            return type;
        }

        // The n-th value after a flag in the probe's flags only, or null.
        public static string ProbeArg(string name, int n = 1)
        {
            int index = Array.IndexOf(ProbeArgs, name);
            return index >= 0 && index + n < ProbeArgs.Length ? ProbeArgs[index + n] : null;
        }

        // The value after a flag on the command line or in the probe's flags, or null.
        public static string ArgValue(string name)
        {
            foreach (string[] args in new[] { CommandLine, ProbeArgs })
            {
                int index = Array.IndexOf(args, name);
                if (index >= 0 && index + 1 < args.Length) return args[index + 1];
            }
            return null;
        }

        // The game's camera keeps Post-processing v2's layer on in every room, though only a few
        // rooms have a volume (color grading). Without effects the layer still renders the
        // camera into a texture and copies it to the screen: on the PS Vita that took 15 ms of
        // a frame in the title room (29 fps with the layer, 55 without). There the layer is on
        // only while a volume is active. Called after RoomComponent.SetPostProcessingVolume,
        // through which room loads and the game's scripts switch volumes.
        public static void PostProcessingChanged()
        {
            if (Application.platform != RuntimePlatform.PSP2) return;
            Type volume = FindType("UnityEngine.Rendering.PostProcessing.PostProcessVolume, Unity.Postprocessing.Runtime");
            if (volume == null) return;
            bool active = false;
            foreach (Object found in Object.FindObjectsOfType(volume))
                if (((Behaviour)found).enabled) { active = true; break; }
            foreach (Camera camera in Camera.allCameras)
            {
                var layer = camera.GetComponent("PostProcessLayer") as Behaviour;
                if (layer != null) layer.enabled = active;
            }
        }

        // The game ships InControl's Windows build, whose platform name is the Windows version
        // on every platform. Its device profiles select platforms by name ("PSP2OS" for the
        // PS Vita), so without this the Vita's controls are an unknown device.
        // Also on Windows players previewing the Vita's controls (see Controls.Emulated).
        public static string InputPlatform(string name)
        {
            return Controls.Emulated ? "PS VITA PSP2OS" : name;
        }

        // InControl's Windows build skips joysticks without a name only on Windows. The PS Vita
        // player reports its controls and three empty slots, which would be unknown devices.
        // A Windows player previewing the Vita's controls reports the Vita's controls instead of
        // its joysticks.
        public static string[] JoystickNames(string[] names)
        {
            if (!Controls.Emulated) return names;
            if (Application.platform != RuntimePlatform.PSP2) return new[] { "PS Vita" };
            int count = names.Length;
            while (count > 0 && string.IsNullOrEmpty(names[count - 1])) count--;
            if (count == names.Length) return names;
            var kept = new string[count];
            System.Array.Copy(names, kept, count);
            return kept;
        }
    }

    // PS Vita controls. The Vita has no triggers, which the game uses most (LT: inventory
    // wheel and equip; RT: use and skip dialog):
    // - its L and R are the triggers: the downgrader retargets them in InControl's Vita profile;
    // - touching the front screen's left or right half holds the bumpers (LB/RB: previous and
    //   next item or dialog option, also on the D-pad). The profile reads them as joystick
    //   buttons 20 and 21, which UnityInputDevice.ReadRawButtonState asks here (Unity has
    //   buttons 0-19, and InControl reads higher ones as released);
    // - touches are not a mouse: the PS Vita player moves the mouse to every touch and clicks
    //   with it (Input.simulateMouseWithTouches = false does not stop it there), and the game
    //   switches to mouse controls (cursor, mouse prompts) when the mouse moves. The
    //   downgrader filters the game's and InControl's mouse reads through MousePosition and
    //   MouseButton: on the Vita the mouse stays where it is without touches, at (0, 0), and
    //   its buttons stay released;
    // Prompts: the game shows its PlayStation glyphs for the Vita (InControl's PlayStationVita
    // style). Their trigger and bumper glyphs become the pipeline's Vita glyphs
    // (vita_glyphs.py, in Resources/VitaGlyphs): L and R shoulder buttons for L2/R2, the
    // front touch screen's halves for L1/R1. See Glyph.
    // Previews (diag builds; the probe's flags): -vita-emulate makes a Windows player
    // behave like the Vita here (Emulated): InControl gets the Vita's platform name and controller instead of its
    // native input and the PC's joysticks, the mouse is filtered, and the prompts show the
    // Vita glyphs. -vita-press "<button>@<from>[-<to>],..." holds Vita buttons (cross,
    // circle, square, triangle, l, r, select, start, up, right, down, left, touchl, touchr)
    // for those seconds after each scene load, except in the title room; 0.2 s by default.
    public static class Controls
    {
        public const int TouchLeftButton = 20, TouchRightButton = 21;
        // A press lasts this long when -vita-press gives no end (seconds).
        const float DefaultPress = 0.2f;
        static bool vita, emulated;
        static int buttonFrame = -1;
        // This frame's virtual buttons, one bit each (indexes 0-63).
        static ulong buttons;
        const int ButtonBits = 64;
        static int[] pressButton;
        static float[] pressFrom, pressTo;
        // Stick deflections in -vita-press: "lx+", "lx-", "ly+", "ly-", "rx+", ... hold that axis
        // of the Vita's sticks (Unity's "joystick N analog A": left stick 0 and 1, right stick 3
        // and 4) at +1 or -1 (InputKeys.GetAxisRaw), so the game's own stick path runs.
        static int[] pressAxis;
        static float[] pressSign;
        static readonly string[] axisNames = { "lx", "ly", "", "rx", "ry" };
        public static bool HasAxisPresses;
        static readonly string[] buttonNames =
        {
            "cross", "circle", "square", "triangle", "l", "r", "select", "start", "up", "right", "down", "left",
            "", "", "", "", "", "", "", "", "touchl", "touchr",
        };

        // The preview flags are read from the probe's flags only (Platform.ProbeArgs), so release
        // players ignore them; again when the probe hands them over (after the first scene).
        static string[] installedFor;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            if (!Platform.NewArgs(ref installedFor)) return;
            vita = Application.platform == RuntimePlatform.PSP2;
            emulated = vita || Array.IndexOf(Platform.ProbeArgs, "-vita-emulate") >= 0;
            string presses = Platform.ProbeArg("-vita-press");
            pressButton = null;
            HasAxisPresses = false;
            if (presses == null) return;
            var parts = presses.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries);
            pressButton = new int[parts.Length];
            pressFrom = new float[parts.Length];
            pressTo = new float[parts.Length];
            pressAxis = new int[parts.Length];
            pressSign = new float[parts.Length];
            var invariant = System.Globalization.CultureInfo.InvariantCulture;
            for (int i = 0; i < parts.Length; i++)
            {
                string[] at = parts[i].Trim().Split('@');
                string[] span = at[1].Split('-');
                string name = at[0].ToLowerInvariant();
                pressButton[i] = Array.IndexOf(buttonNames, name);
                pressAxis[i] = -1;
                if (pressButton[i] < 0 && name.Length == 3 && (name[2] == '+' || name[2] == '-'))
                {
                    pressAxis[i] = Array.IndexOf(axisNames, name.Substring(0, 2));
                    pressSign[i] = name[2] == '+' ? 1f : -1f;
                    HasAxisPresses |= pressAxis[i] >= 0;
                }
                pressFrom[i] = float.Parse(span[0], invariant);
                pressTo[i] = span.Length > 1 ? float.Parse(span[1], invariant) : pressFrom[i] + DefaultPress;
            }
        }

        // Presses apply outside the title room. The active scene's name is looked up only when
        // its handle (GetHashCode) changes (Scene.name allocates a string on every read).
        static int pressScene = -1;
        static bool pressSceneIsTitle;

        static bool PressesActive()
        {
            UnityEngine.SceneManagement.Scene scene = UnityEngine.SceneManagement.SceneManager.GetActiveScene();
            if (scene.GetHashCode() != pressScene)
            {
                pressScene = scene.GetHashCode();  // the scene handle
                pressSceneIsTitle = scene.name == "SceneRoomTitle";
            }
            return !pressSceneIsTitle;
        }

        // InputKeys.GetAxisRaw("joystick N analog A"): a -vita-press stick deflection of axis A
        // held now (outside the title room), else the engine's value.
        public static float VirtualAxis(string name, float value)
        {
            if (!HasAxisPresses || !Emulated) return value;
            int analog = name.LastIndexOf("analog ", StringComparison.Ordinal);
            if (analog < 0 || analog + 8 != name.Length) return value;
            int axis = name[name.Length - 1] - '0';
            if (!PressesActive()) return value;
            float time = Time.timeSinceLevelLoad;
            for (int i = 0; i < pressAxis.Length; i++)
                if (pressAxis[i] == axis && time >= pressFrom[i] && time < pressTo[i]) return pressSign[i];
            return value;
        }

        public static bool Emulated
        {
            get
            {
                Install();
                return emulated;
            }
        }

        // After Input.mousePosition.
        public static Vector3 MousePosition(Vector3 position)
        {
            return Emulated ? Vector3.zero : position;
        }

        // After Input.GetMouseButton, GetMouseButtonDown, GetMouseButtonUp and mousePresent.
        public static bool MouseButton(bool value)
        {
            return value && !Emulated;
        }

        // After InputManager.EnableNativeInput: a preview reads the Vita's controller through
        // Unity's input, as the Vita does.
        public static bool NativeInput(bool enabled)
        {
            return enabled && !Emulated;
        }

        // Skipping the intro with Cross on the Vita, through the game's own skips:
        // - the logos (LoadingScreen.Update): while one shows and the title room has loaded,
        //   Escape, Period, Return or the left mouse button held skips the rest. LogoSkipKey
        //   follows its Input.GetKey(KeyCode.Return): Cross held counts too, and so does a
        //   Cross press since the logos began (LogoFrame, at the start of each Update). On the
        //   Vita a Cross press skips without waiting for the title room (LogoSkip, after
        //   that check's m_ready): the logo fades out as in the game's skip and the screen
        //   stays black until the room has loaded (the game's own path once all logos are
        //   done). A press before the first logo shows none (LogoSkip, after
        //   SetState's read of m_skip);
        // - the title room's intro is a cutscene, which SkipCutscene (Return; Select, Back on
        //   controllers) skips. SkipCutscenePressed follows SystemInput.UpdateNoPause's first
        //   Buttons.SkipCutscene.WasPressed: in the title room a Cross press (the frame it goes
        //   down) counts too, but only during the title room's intro (TitleIntroStarted and
        //   TitleIntroEnded, after the StartCutscene and EndCutscene of
        //   RoomTitle.OnEnterRoomAfterFade): its New Game sequence is a cutscene in the same
        //   room, where Cross skips one line (SkipDialog), as in the game. The game's menu submits with Return or Menu Ok pressed, or Use
        //   held (GlobalScript.UpdateNoPause, before the skip each frame), and the menu shows
        //   as soon as the intro is skipped: after a skip with Cross (CutsceneSkipped, after
        //   SystemInput's first PowerQuest.SkipCutscene()) the submit ignores Menu Ok and Use
        //   (AfterIntroSkip) until Cross is released.
        // The logo press is never cleared: only LoadingScreen reads it (LogoSkip, LogoSkipKey), and
        // it runs once, in the first scene, which no later scene load brings back.
        static bool logoSkipPressed;

        public static void LogoFrame()
        {
            if (vita && Input.GetKeyDown(KeyCode.JoystickButton0)) logoSkipPressed = true;
        }

        public static bool LogoSkip(bool value)
        {
            return value || (vita && logoSkipPressed);
        }

        public static bool LogoSkipKey(bool held)
        {
            return held || (vita && (logoSkipPressed || Input.GetKey(KeyCode.JoystickButton0)));
        }

        static bool introSkipHeld;

        public static bool CutsceneSkipped(bool skipped)
        {
            if (skipped && vita && Input.GetKeyDown(KeyCode.JoystickButton0)) introSkipHeld = true;
            return skipped;
        }

        public static bool AfterIntroSkip(bool value)
        {
            if (introSkipHeld && !Input.GetKey(KeyCode.JoystickButton0)) introSkipHeld = false;
            return value && !introSkipHeld;
        }

        static bool titleIntro;

        public static void TitleIntroStarted() { titleIntro = true; }

        public static void TitleIntroEnded() { titleIntro = false; }

        public static bool SkipCutscenePressed(bool pressed)
        {
            return pressed || (vita && titleIntro && Input.GetKeyDown(KeyCode.JoystickButton0));
        }

        // UnityInputDevice.ReadRawButtonState(index) returns true when this does: the touch
        // halves (buttons 20 and 21) and a preview's scripted presses.
        public static bool VirtualButton(int index)
        {
            if (!Emulated || index < 0 || index >= ButtonBits) return false;
            if (buttonFrame != Time.frameCount)
            {
                buttonFrame = Time.frameCount;
                buttons = 0;
                if (vita)
                {
                    float half = Screen.width * 0.5f;
                    for (int i = 0, count = Input.touchCount; i < count; i++)
                    {
                        Touch touch = Input.GetTouch(i);
                        if (touch.phase == TouchPhase.Ended || touch.phase == TouchPhase.Canceled) continue;
                        buttons |= 1UL << (touch.position.x < half ? TouchLeftButton : TouchRightButton);
                    }
                }
                if (pressButton != null && PressesActive())
                {
                    float time = Time.timeSinceLevelLoad;
                    for (int i = 0; i < pressButton.Length; i++)
                        if (pressButton[i] >= 0 && time >= pressFrom[i] && time < pressTo[i]) buttons |= 1UL << pressButton[i];
                }
            }
            return (buttons >> index & 1UL) != 0;
        }

        // The game's texts (QuestText.EvaluateImageTagMatch, the sprite of a [tag]) and
        // GuiPlatformContent (its PlayStation sprite) show Glyph(sprite). The sprites of the
        // text tags [LT], [RT], [LB], [RB] and [START] for "ps" (PowerQuest.TextSprites, read
        // by reflection: the game's types are not visible from here) become VitaL, VitaR,
        // VitaTouchL, VitaTouchR and VitaStart. GuiPlatformContent shows gui images by sprite name, which
        // PowerQuest looks up in its gui sprite list: the Vita glyphs are added to it. All
        // again (at most once a second) for a new PowerQuest. The controller picture is known
        // by name when GuiPlatformContent first shows it; other sprites are checked once.
        static readonly string[] glyphTags = { "LT", "RT", "LB", "RB", "START" };
        // The controls screen's picture of the PlayStation controller (GuiPlatformContent, Ps_0)
        // becomes the Vita's (vita_controls_art.py).
        static readonly string[] glyphNames = { "VitaL", "VitaR", "VitaTouchL", "VitaTouchR", "VitaStart", "VitaControls" };
        const string ControllerPicture = "Ps_0";
        static Sprite[] glyphFrom, glyphTo;
        static Object glyphQuest;
        static int glyphFrame = -1;
        // Frames between looks for a new PowerQuest (FindObjectOfType) while there is none.
        const int GlyphRetryFrames = 60;
        static readonly HashSet<int> notPicture = new HashSet<int>();

        // The texts' glyph platform (QuestText.s_textSpritePlatform, read in FindByTag) is set by
        // SystemInput.UpdateInput from the active device's style, but only during play: until
        // then, as on the title screen and its menus, texts show the default (Xbox) glyphs, such
        // as LB and RB. On the Vita (and in previews) it is always "ps".
        public static string TextSpritePlatform(string platform)
        {
            return Emulated ? "ps" : platform;
        }

        public static Sprite Glyph(Sprite sprite)
        {
            if (!Emulated || (object)sprite == null || !MainThread.Current) return sprite;
            if (glyphQuest == null && (glyphFrame < 0 || Time.frameCount - glyphFrame >= GlyphRetryFrames))
            {
                glyphFrame = Time.frameCount;
                ResolveGlyphs();
            }
            if (glyphQuest == null) return sprite;
            int id = sprite.GetInstanceID();
            int picture = glyphTags.Length;
            if ((object)glyphFrom[picture] == null && !notPicture.Contains(id))
            {
                if (sprite.name == ControllerPicture) glyphFrom[picture] = sprite;
                else notPicture.Add(id);
            }
            for (int i = 0; i < glyphFrom.Length; i++)
                if ((object)glyphFrom[i] != null && (object)glyphTo[i] != null && glyphFrom[i].GetInstanceID() == id)
                    return glyphTo[i];
            return sprite;
        }

        static void ResolveGlyphs()
        {
            glyphQuest = null;
            Type quest = Platform.FindType(Platform.PowerQuestType);
            Object instance = quest == null ? null : Object.FindObjectOfType(quest);
            var textSprites = instance == null ? null : quest.GetProperty("TextSprites").GetValue(instance, null) as Array;
            var guiSprites = instance == null ? null : quest.GetMethod("GetGuiSprites").Invoke(instance, null) as List<Sprite>;
            if (textSprites == null || guiSprites == null) return;
            if (glyphTo == null)
            {
                glyphTo = new Sprite[glyphNames.Length];
                for (int i = 0; i < glyphNames.Length; i++)
                    glyphTo[i] = Resources.Load<Sprite>("VitaGlyphs/" + glyphNames[i]);
            }
            glyphFrom = new Sprite[glyphNames.Length];
            notPicture.Clear();
            foreach (object data in textSprites)
            {
                if (data == null) continue;
                Type type = data.GetType();
                if (!string.Equals(type.GetField("m_platform").GetValue(data) as string, "ps", StringComparison.OrdinalIgnoreCase)) continue;
                int index = Array.IndexOf(glyphTags, ((string)type.GetField("m_tag").GetValue(data) ?? "").ToUpperInvariant());
                if (index >= 0) glyphFrom[index] = type.GetField("m_sprite").GetValue(data) as Sprite;
            }
            foreach (Sprite glyph in glyphTo)
                if (glyph != null && !guiSprites.Contains(glyph)) guiSprites.Add(glyph);
            glyphQuest = instance;
        }
    }

    // Crt's render hook (the filter's paths are described at Crt, in compat/Crt.cs).
    public delegate void CrtRender(RenderTexture source, RenderTexture destination);

    // The game toggles its CRT filter with Shift+F (GlobalScript.Update: CameraFilterOn flips
    // and a prompt says so), a debug key. The PS Vita has no keyboard: there Select + R
    // (R pressed while Select is held) flips it, silently, once per press, between the modes
    // off and patched (on, with Compat Crt's single pass, the Vita's default path). The
    // combo's R (the game's right trigger) does not reach the game:
    // UnityInputDevice.ReadRawButtonState reads button 5 as released from the press until R
    // is let go (SuppressButton). Select keeps its own action (skip a cutscene or the current
    // dialog line, on press), so the combo is meant for outside dialog. After a change the
    // settings are saved and the mode's name is remembered (CrtChoice.txt beside the saves),
    // so from then on the game's 540-line limit applies (Platform.LowResolutionLines) and the
    // saved setting and the single pass are kept across restarts. Each change logs
    // "Crt mode: <name>". -compat-crt-mode off|patched (tests; on the Vita from the probe's
    // flags) holds a mode without saving anything. Main thread only.
    public static class CrtToggle
    {
        const string ChoiceFile = "CrtChoice.txt";
        // The modes' names, in CrtChoice.txt, the log and -compat-crt-mode.
        const string Patched = "patched", Off = "off";
        // R is joystick button 5 (InControl's index for it too), Select button 6.
        const int R = 5;
        const KeyCode RKey = KeyCode.JoystickButton0 + R, SelectKey = KeyCode.JoystickButton6;
        // A held mode is checked every this many frames.
        const int HoldFrames = 30;
        static bool vita, pending, chosen, chosenRead, suppressing, loaded;
        static int readFrame = -1;
        static string[] forcedFor;
        static bool? forced;
        static Object questInstance;

        // -compat-crt-mode holds a mode.
        public static bool Forced
        {
            get { return forced != null || Platform.HasArg("-compat-crt-mode"); }
        }

        public static bool Chosen
        {
            get
            {
                if (!chosenRead)
                {
                    chosenRead = true;
                    // Only with the settings it was made for: new settings (no Settings.sav)
                    // start with the filter off, as on a first start.
                    if (Application.platform == RuntimePlatform.PSP2)
                        try
                        {
                            chosen = System.IO.File.Exists(Platform.CurrentDirectory() + ChoiceFile) &&
                                     System.IO.File.Exists(Platform.CurrentDirectory() + "Settings.sav");
                        }
                        catch (Exception) { chosen = false; }
                }
                return chosen;
            }
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            vita = Application.platform == RuntimePlatform.PSP2;
            // Everywhere, for -compat-crt-mode; the combo is the Vita's only.
            UpdateHost.Create("CrtToggle", Poll);
        }

        // Reads the buttons once a frame.
        static void Read()
        {
            if (!vita || readFrame == Time.frameCount) return;
            readFrame = Time.frameCount;
            bool select = Input.GetKey(SelectKey), r = Input.GetKey(RKey);
            if (select && Input.GetKeyDown(RKey)) pending = true;
            if (select && r) suppressing = true;
            else if (!r) suppressing = false;
        }

        // UnityInputDevice.ReadRawButtonState(index) answers released when this is true.
        public static bool SuppressButton(int index)
        {
            if (index != R || !vita) return false;
            Read();
            return suppressing;
        }

        internal static void Poll()
        {
            Read();
            if (!loaded && vita)
            {
                // The remembered mode's single pass (the setting itself comes back with Settings.sav).
                loaded = true;
                if (Chosen)
                    try
                    {
                        if (System.IO.File.ReadAllText(Platform.CurrentDirectory() + ChoiceFile).Trim() == Patched) Crt.UseSinglePass();
                    }
                    catch (Exception) { }
            }
            if (Platform.NewArgs(ref forcedFor))
            {
                string name = Platform.ArgValue("-compat-crt-mode");
                forced = name == Patched ? true : name == Off ? false : (bool?)null;
                if (name != null) Debug.Log(forced == null ? "-compat-crt-mode: no mode " + name : "Crt mode held: " + name);
            }
            // Held: again whenever the game's setting or the single pass no longer match (the
            // settings load at the title, the game's resolution check). A build without the
            // single pass's shader keeps the original passes: only the setting is held then.
            if (forced != null && Time.frameCount % HoldFrames == 0 &&
                (FilterOn != forced.Value || (forced.Value && !Crt.SinglePass && !Crt.ShaderMissing)))
                Apply(forced.Value, "-compat-crt-mode", false);
            if (!pending) return;
            pending = false;
            Flip("Select + R");
        }

        static Object Quest(out Type quest)
        {
            quest = Platform.FindType(Platform.PowerQuestType);
            if (questInstance == null && quest != null) questInstance = Object.FindObjectOfType(quest);
            return questInstance;
        }

        // QuestSettings.CameraFilterOn.
        public static bool FilterOn
        {
            get
            {
                Type quest;
                Object instance = Quest(out quest);
                if (instance == null) return false;
                object settings = quest.GetProperty("Settings").GetValue(instance, null);
                return (bool)settings.GetType().GetProperty("CameraFilterOn").GetValue(settings, null);
            }
        }

        // Flips the setting as Shift+F does, without its prompt (the combo and the options entry,
        // VitaOptions).
        public static void Flip(string by)
        {
            Apply(!FilterOn, by, true);
        }

        // Sets the game's setting, and on, Crt's single pass; with save, saves the settings and
        // remembers the mode.
        static void Apply(bool on, string by, bool save)
        {
            Type quest;
            Object instance = Quest(out quest);
            if (instance == null) return;
            object settings = quest.GetProperty("Settings").GetValue(instance, null);
            // The setter turns the GUI camera's CRTEffect on or off 0.01 s of game time later,
            // which in the options menu (the game paused) waited until the menu closed: the
            // label changed but the picture did not. The effect follows at once here; it goes
            // off first, so that Crt.Disabled and Enabled undo and set up the paths.
            var gui = quest.GetMethod("GetCameraGui", Type.EmptyTypes);
            var camera = gui == null ? null : gui.Invoke(instance, null) as Camera;
            var effect = camera == null ? null : camera.GetComponent("CRTEffect") as Behaviour;
            if (effect != null) effect.enabled = false;
            if (on) Crt.UseSinglePass();
            settings.GetType().GetProperty("CameraFilterOn").SetValue(settings, on, null);
            if (effect != null) effect.enabled = on;
            string name = on ? Patched : Off;
            Debug.Log("Crt mode: " + name + " (" + by + ")");
            if (!save) return;
            chosen = chosenRead = true;
            try { System.IO.File.WriteAllText(Platform.CurrentDirectory() + ChoiceFile, name); }
            catch (Exception e) { Debug.Log("CRT toggle: " + e.Message); }
            quest.GetMethod("SaveSettings", Type.EmptyTypes).Invoke(instance, null);
        }
    }

    // The PS Vita's options menu. The game has a console layout of its menus for the Switch
    // (GlobalScript.PlatformSwitch, see Platform.ConsoleUi): no Quit in the main and pause
    // menus or after a chapter, no window, resolution or frame-rate options. Its Fullscreen
    // entry becomes the CRT filter's: the downgrader keeps it in the options grid
    // (KeepFullscreen), its click flips the filter (FullscreenClick, CrtToggle.Flip) and
    // GuiOptions.UpdateText ends with Labels, which writes "CRT Filter: On/Off" on it. The
    // label is English in every language ("CRT Filter: {0}" is not in the game's texts; On
    // and Off are, 2148 and 2149).
    public static class VitaOptions
    {
        static bool Vita { get { return Application.platform == RuntimePlatform.PSP2; } }

        // GuiOptions.OnShow skips its Container("GridContainer").RemoveFromGrid("Fullscreen")
        // statement when this is true.
        public static bool KeepFullscreen()
        {
            return Vita;
        }

        // The start of GuiOptions.OnClickFullscreen: on the Vita the coroutine to run instead.
        public static System.Collections.IEnumerator FullscreenClick(object gui)
        {
            if (!Vita) return null;
            CrtToggle.Flip("options");
            Labels(gui);
            return Nothing();
        }

        static System.Collections.IEnumerator Nothing()
        {
            yield break;
        }

        // The end of GuiOptions.UpdateText.
        public static void Labels(object gui)
        {
            if (!Vita) return;
            Type text = Platform.FindType(Platform.SystemTextType);
            var localize = text == null ? null : text.GetMethod("Localize", new[] { typeof(string), typeof(int), typeof(string) });
            Func<string, int, string> local = (value, id) => localize == null ? value : (string)localize.Invoke(null, new object[] { value, id, null });
            string label = string.Format(local("CRT Filter: {0}", -1), CrtToggle.FilterOn ? local("On", 2148) : local("Off", 2149));
            object button = gui.GetType().GetMethod("Button", new[] { typeof(string) }).Invoke(gui, new object[] { "Fullscreen" });
            if (button != null) button.GetType().GetProperty("Text").SetValue(button, label, null);
        }
    }

    // A hidden object that lives across scenes and calls its poll every frame (CrtToggle, PowerTicks).
    public class UpdateHost : MonoBehaviour
    {
        Action poll;

        void Update()
        {
            poll();
        }

        internal static void Create(string name, Action poll)
        {
            var host = new GameObject(name);
            host.hideFlags = HideFlags.HideAndDontSave;
            Object.DontDestroyOnLoad(host);
            host.AddComponent<UpdateHost>().poll = poll;
        }
    }

    // The PS Vita suspends itself after the system's auto-standby time without input, even
    // while a game runs (Screen.sleepTimeout does nothing on PSP2); a game that should keep
    // running calls sceKernelPowerTick. Every 5 s this calls the player's
    // UnityEngine.PSVita.Utility.PowerTick(DisableAutoSuspend) (through reflection, since Compat
    // is built against the editor's engine; link.xml keeps it): the screen still dims and
    // turns off as the system settings say, but the console no longer suspends the game.
    // -compat-keep-screen-on ticks Default instead, which also keeps the screen on (for
    // watching or testing hands-off); -compat-allow-sleep turns the rule off. Main thread only.
    public static class PowerTicks
    {
        // Seconds between ticks: far shorter than the system's auto-standby times (minutes).
        const float Interval = 5f;
        static System.Reflection.MethodInfo tick;
        static object[] args;
        static Type kindType;
        static string[] configuredFor;
        static float next;
        static bool logged, allowSleep;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            if (Application.platform != RuntimePlatform.PSP2) return;
            Type utility = Platform.FindType(Platform.VitaUtilityType);
            Type kind = Type.GetType("UnityEngine.PSVita.Utility+PowerTickType, UnityEngine.PSVitaModule");
            if (utility == null || kind == null)
            {
                Debug.Log("PowerTicks: UnityEngine.PSVita.Utility.PowerTick missing");
                return;
            }
            tick = utility.GetMethod("PowerTick", new[] { kind });
            if (tick == null) return;
            kindType = kind;
            UpdateHost.Create("PowerTicks", Poll);
        }

        // The flags are read again whenever the probe hands over its own (Platform.NewArgs).
        static bool Configure()
        {
            if (!Platform.NewArgs(ref configuredFor)) return !allowSleep;
            allowSleep = Platform.HasArg("-compat-allow-sleep");
            args = new[] { Enum.ToObject(kindType, Platform.HasArg("-compat-keep-screen-on") ? 0 : 1) };
            logged = false;
            return !allowSleep;
        }

        public static void Poll()
        {
            float now = Time.realtimeSinceStartup;
            if (now < next) return;
            next = now + Interval;
            if (!Configure()) return;
            try
            {
                tick.Invoke(null, args);
                if (!logged)
                {
                    logged = true;
                    Debug.Log("PowerTicks: ticking " + args[0] + " every " + Interval + " s");
                }
            }
            catch (Exception e)
            {
                Debug.Log("PowerTicks: " + e.GetType().Name + " " + e.Message);
                next = float.MaxValue;
            }
        }
    }

    // PS Vita: the player's heaps (sceHeap mspaces that grow by whole blocks taken from the
    // system) took the first free space in whichever block they came to, so freed space stayed
    // scattered over many blocks and none of them emptied to go back to the system. Free main
    // memory fell 26 MB over three rounds of an 11-room chain with dialog voices and sound cues
    // (124 to 98 MB) while Unity's own allocations grew 8 MB (the cues' audio data, see
    // AudioData); 18 MB was that slack, and 7 MB of it came on the first round even without the
    // voices and cues. The player's Utility.EnableHeapBlockSorting (allocation mode 1 for its
    // heaps: the blocks are kept sorted, so allocations fill the lowest ones first) is called
    // when the first scene unloads (the loading screen's, before the title room; the probe's
    // flags are known by then): the same three rounds then lost 11 MB (124 to 113), all but 3 MB
    // of it the audio data, with the same frame rate in the rooms and the same room-change times;
    // five rounds (11 minutes) ended at 109 MB free, against 93 MB without it.
    // Off with -compat-no-heap-sorting. (Giving the heap the 26 MB of physically contiguous
    // memory kept for video, PSVitaVideoPlayer.TransferMemToHeap, was measured: free main memory
    // fell as without it.)
    // (Utility.SetMonoHeapBehaviours is an empty function in this IL2CPP player; the GC heap
    // stayed at 50.9 MB through every run.)
    public static class MemorySetup
    {
        static bool done;

        // When a scene unloads (SceneUnloads).
        internal static void Apply()
        {
            if (done || Application.platform != RuntimePlatform.PSP2) return;
            done = true;
            Type utility = Platform.FindType(Platform.VitaUtilityType);
            if (Platform.HasArg("-compat-no-heap-sorting") || utility == null) return;
            try
            {
                utility.GetMethod("EnableHeapBlockSorting").Invoke(null, null);
                Debug.Log("MemorySetup: heap block sorting");
            }
            catch (Exception e)
            {
                Debug.Log("MemorySetup: " + e.GetType().Name + " " + e.Message);
            }
        }
    }
}
