using System;
using System.Collections.Generic;
using UnityEngine;
using Object = UnityEngine.Object;

// Audio: filter components and reverbs per source, clip data budget.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // PS Vita: SystemAudio gives every pooled AudioSource six filter components (low pass, high
    // pass, echo, chorus, distortion, reverb), disabled unless a cue asks for one. The Vita's
    // player builds a source's filter chain each time it starts or is activated, even for
    // disabled filters: a footstep's Play took 13-20 ms of the main thread with the six (10.5 ms
    // of it for the reverb filter alone), 0.3 ms with none (measured on the Vita).
    // There SystemAudio configures the filters on a stand-in object (inactive, one per source,
    // never heard), and when it starts the source the source gets exactly the filters that are
    // enabled there, in SystemAudio's order and with their settings; the others are removed, and
    // all are removed before SystemAudio deactivates a source. Other game code asking a source
    // for a filter it lacks gets one made from the stand-in's (disabled unless enabled there).
    //
    // Reverb: one FMOD SFX reverb takes about 17% of the Vita's audio mixer core whatever its
    // settings (density, diffusion and decay time from 0/0/0.1 s to 100/100/20 s all measured
    // the same), against 45-50% for the mixer with music and ambience playing. The game ran one
    // in the mixer for dialog's room reverb (RoomVerb, fed by a send from the Dialog group)
    // whenever any sound played, and one per voice for its 700 cues with a reverb preset
    // (footsteps, typing, ambiences), so the mixer ran late and its output short (crackling,
    // above all during dialog while walking: 45-570 ms short every 5 s). On the Vita the pipeline
    // bypasses RoomVerb's reverb and the send to it (MIXER_SEND_REVERB_OFF), so dialog has
    // no room reverb, as Vita builds had before the mixer's parameter names were restored, and
    // at most VitaReverbLimit voices have their own reverb at a time; later ones play dry until
    // one ends (the presets keep the dry signal at full level). With one, the worst case measured
    // (dialog, music, ambience and footsteps, in LabKitchen and on the Bridge) left the core at
    // most 74% busy (the Bridge; LabKitchen 68%); with two, 76% in LabKitchen.
    //
    // The downgrader sends SystemAudio's GetComponent/AddComponent of the six filter types,
    // AudioSource.Play/PlayDelayed and GameObject.SetActive here, and other game code's
    // GetComponent of a filter to Live. Elsewhere than on the Vita every call does what the original did. Main thread only.
    public static class SourceFilters
    {
        static readonly Type[] Kinds = { typeof(AudioLowPassFilter), typeof(AudioHighPassFilter), typeof(AudioEchoFilter),
            typeof(AudioChorusFilter), typeof(AudioDistortionFilter), typeof(AudioReverbFilter) };
        const int Reverb = 5;
        // By source (instance id). Kept for the session: the sources are SystemAudio's pool,
        // which lives as long as the game.
        static readonly Dictionary<int, GameObject> standIns = new Dictionary<int, GameObject>();
        static readonly Behaviour[] current = new Behaviour[Kinds.Length];
        static GameObject root;

        static bool Active { get { return Application.platform == RuntimePlatform.PSP2; } }

        static GameObject StandIn(GameObject source, bool create)
        {
            GameObject standIn;
            if (standIns.TryGetValue(source.GetInstanceID(), out standIn) && standIn != null) return standIn;
            if (!create) return null;
            if (root == null)
            {
                root = new GameObject("DowngradeFilterStandIns");
                root.SetActive(false);
                Object.DontDestroyOnLoad(root);
            }
            standIn = new GameObject(source.name);
            standIn.transform.parent = root.transform;
            standIn.AddComponent<AudioSource>();
            standIns[source.GetInstanceID()] = standIn;
            return standIn;
        }

        public static T StageGet<T>(GameObject source) where T : Behaviour
        {
            if (!Active) return source.GetComponent<T>();
            GameObject standIn = StandIn(source, false);
            return standIn == null ? null : standIn.GetComponent<T>();
        }

        public static T StageAdd<T>(GameObject source) where T : Behaviour
        {
            if (!Active) return source.AddComponent<T>();
            return StandIn(source, true).AddComponent<T>();
        }

        public static void SetActive(GameObject gameObject, bool value)
        {
            if (Active && !value && gameObject.activeSelf && StandIn(gameObject, false) != null) Strip(gameObject);
            gameObject.SetActive(value);
        }

        public static void Play(AudioSource source)
        {
            if (Active) Apply(source);
            AudioData.Played(source);
            source.Play();
        }

        public static void PlayDelayed(AudioSource source, float delay)
        {
            if (Active) Apply(source);
            AudioData.Played(source);
            source.PlayDelayed(delay);
        }

        public static T Live<T>(Component component) where T : Behaviour
        {
            if (!Active) return component.GetComponent<T>();
            return LiveOn<T>(component.gameObject);
        }

        public static T LiveGameObject<T>(GameObject gameObject) where T : Behaviour
        {
            if (!Active) return gameObject.GetComponent<T>();
            return LiveOn<T>(gameObject);
        }

        static T LiveOn<T>(GameObject gameObject) where T : Behaviour
        {
            T found = gameObject.GetComponent<T>();
            GameObject standIn = found == null ? StandIn(gameObject, false) : null;
            T staged = standIn == null ? null : standIn.GetComponent<T>();
            if (staged == null) return found;
            // Rebuilt in SystemAudio's order, the filters already there keeping their state.
            int kind = Array.IndexOf(Kinds, typeof(T));
            var keep = new Behaviour[Kinds.Length];
            GameObject scratch = new GameObject("DowngradeFilterScratch");
            scratch.SetActive(false);
            scratch.AddComponent<AudioSource>();
            for (int k = kind + 1; k < Kinds.Length; k++)
            {
                var existing = gameObject.GetComponent(Kinds[k]) as Behaviour;
                if (existing == null) continue;
                keep[k] = (Behaviour)scratch.AddComponent(Kinds[k]);
                Copy(existing, keep[k]);
                Object.DestroyImmediate(existing);
            }
            T made = gameObject.AddComponent<T>();
            Copy(staged, made);
            for (int k = kind + 1; k < Kinds.Length; k++)
                if (keep[k] != null) Copy(keep[k], (Behaviour)gameObject.AddComponent(Kinds[k]));
            Object.Destroy(scratch);
            return made;
        }

        static void Strip(GameObject gameObject)
        {
            for (int k = 0; k < Kinds.Length; k++)
            {
                Component existing = gameObject.GetComponent(Kinds[k]);
                if (existing != null) Object.DestroyImmediate(existing);
            }
        }

        static void Apply(AudioSource source)
        {
            GameObject gameObject = source.gameObject;
            GameObject standIn = StandIn(gameObject, false);
            bool same = true;
            var wanted = new Behaviour[Kinds.Length];
            for (int k = 0; k < Kinds.Length; k++)
            {
                var staged = standIn == null ? null : standIn.GetComponent(Kinds[k]) as Behaviour;
                wanted[k] = staged != null && staged.enabled ? staged : null;
                current[k] = gameObject.GetComponent(Kinds[k]) as Behaviour;
            }
            if (!ReverbSlot(source)) wanted[Reverb] = null;
            for (int k = 0; k < Kinds.Length; k++)
                if ((current[k] != null) != (wanted[k] != null)) same = false;
            if (!same)
            {
                Strip(gameObject);
                for (int k = 0; k < Kinds.Length; k++)
                    current[k] = wanted[k] != null ? (Behaviour)gameObject.AddComponent(Kinds[k]) : null;
            }
            for (int k = 0; k < Kinds.Length; k++)
                if (wanted[k] != null) Copy(wanted[k], current[k]);
            if (current[Reverb] != null && !reverbVoices.Contains(source)) reverbVoices.Add(source);
            Array.Clear(current, 0, current.Length);
        }

        // At most ReverbLimit voices have a reverb at a time; later ones play dry (their presets
        // keep the dry signal at full level) until one ends. -compat-reverb-limit <n> sets it,
        // -1 for no limit.
        const int VitaReverbLimit = 1;
        static readonly List<AudioSource> reverbVoices = new List<AudioSource>();
        static int reverbLimit;
        static string[] reverbLimitFor;

        static int ReverbLimit
        {
            get
            {
                if (Platform.NewArgs(ref reverbLimitFor) && !int.TryParse(Platform.ArgValue("-compat-reverb-limit"), out reverbLimit))
                    reverbLimit = VitaReverbLimit;
                return reverbLimit;
            }
        }

        static bool ReverbSlot(AudioSource source)
        {
            int limit = ReverbLimit;
            if (limit < 0) return true;
            int busy = 0;
            for (int i = reverbVoices.Count - 1; i >= 0; i--)
            {
                AudioSource voice = reverbVoices[i];
                var filter = voice == null ? null : voice.GetComponent<AudioReverbFilter>();
                if (filter == null || !filter.enabled || !voice.gameObject.activeInHierarchy || !voice.isPlaying)
                {
                    reverbVoices.RemoveAt(i);
                    continue;
                }
                if (voice != source) busy++;
            }
            reverbVoices.Remove(source);
            return busy < limit;
        }

        static void Copy(Behaviour from, Behaviour to)
        {
            if (from is AudioLowPassFilter)
            {
                var a = (AudioLowPassFilter)from; var b = (AudioLowPassFilter)to;
                b.cutoffFrequency = a.cutoffFrequency;
                b.lowpassResonanceQ = a.lowpassResonanceQ;
            }
            else if (from is AudioHighPassFilter)
            {
                var a = (AudioHighPassFilter)from; var b = (AudioHighPassFilter)to;
                b.cutoffFrequency = a.cutoffFrequency;
                b.highpassResonanceQ = a.highpassResonanceQ;
            }
            else if (from is AudioEchoFilter)
            {
                var a = (AudioEchoFilter)from; var b = (AudioEchoFilter)to;
                b.delay = a.delay;
                b.decayRatio = a.decayRatio;
                b.dryMix = a.dryMix;
                b.wetMix = a.wetMix;
            }
            else if (from is AudioChorusFilter)
            {
                var a = (AudioChorusFilter)from; var b = (AudioChorusFilter)to;
                b.dryMix = a.dryMix;
                b.wetMix1 = a.wetMix1;
                b.wetMix2 = a.wetMix2;
                b.wetMix3 = a.wetMix3;
                b.delay = a.delay;
                b.rate = a.rate;
                b.depth = a.depth;
            }
            else if (from is AudioDistortionFilter)
                ((AudioDistortionFilter)to).distortionLevel = ((AudioDistortionFilter)from).distortionLevel;
            else if (from is AudioReverbFilter)
            {
                var a = (AudioReverbFilter)from; var b = (AudioReverbFilter)to;
                b.reverbPreset = a.reverbPreset;
                if (a.reverbPreset == AudioReverbPreset.User)
                {
                    b.dryLevel = a.dryLevel;
                    b.room = a.room;
                    b.roomHF = a.roomHF;
                    b.roomLF = a.roomLF;
                    b.decayTime = a.decayTime;
                    b.decayHFRatio = a.decayHFRatio;
                    b.reflectionsLevel = a.reflectionsLevel;
                    b.reflectionsDelay = a.reflectionsDelay;
                    b.reverbLevel = a.reverbLevel;
                    b.reverbDelay = a.reverbDelay;
                    b.hfReference = a.hfReference;
                    b.lfReference = a.lfReference;
                    b.diffusion = a.diffusion;
                    b.density = a.density;
                }
            }
            to.enabled = from.enabled;
        }
    }

    // On the PS Vita no clip preloads its data (asset_rules.py PLATFORM_AUDIO): a clip kept in
    // memory loads it when it first plays and then keeps it while the clip object lives. The
    // game's SystemAudio lists every audio cue, so every sound effect, ambience and one-shot
    // ever played stayed in memory: about 1 MB a minute of play (47 minutes: free main memory
    // 136 to 87 MB). When a scene unloads, loaded clips go back to unloaded, least recently
    // played first, until those left fit in the budget (-compat-audio-budget <MB>, default 4,
    // counted as HEVAG, 4 bits a sample). The loaded clips average 36 KB (largest 0.5 MB), so
    // 4 MB still keeps about 100 of them; on the Vita, three rounds of an 11-room chain playing
    // every cue kept 117 MB of main memory free with 4 MB against 113 MB with 10 MB, with no more
    // stalls and the same room-change times. A clip is never unloaded while an active AudioSource
    // holds it (playing, paused or about to play), nor if it streams (music, long loops), nor if
    // it played since the previous scene unload. One that plays again loads again, as on its first
    // play. SystemAudio's plays are stamped through SourceFilters.Play; clips loaded some other
    // way count as played when first seen loaded. Main thread only; keeps instance ids, not
    // references, so unloading the clip objects is unaffected. Off with -compat-keep-audio-data;
    // on Windows (where clips preload) only with -compat-audio-data.
    public static class AudioData
    {
        static readonly Dictionary<int, int> stamps = new Dictionary<int, int>();
        static int clock, unloads, lastUnload;
        static string[] configuredFor;
        static bool active;
        static long budget;
        // -compat-audio-budget's default (MB; the measurements above).
        const float DefaultBudget = 4f, Megabyte = 1048576f;

        static bool Active
        {
            get
            {
                if (!Platform.NewArgs(ref configuredFor)) return active;
                active = Platform.VitaDefault("-compat-keep-audio-data", "-compat-audio-data");
                float megabytes;
                if (!float.TryParse(Platform.ArgValue("-compat-audio-budget"), System.Globalization.NumberStyles.Float,
                                    System.Globalization.CultureInfo.InvariantCulture, out megabytes)) megabytes = DefaultBudget;
                budget = (long)(megabytes * Megabyte);
                return active;
            }
        }

        public static void Played(AudioSource source)
        {
            if (!MainThread.Current || !Active) return;
            AudioClip clip = source.clip;
            if (!ReferenceEquals(clip, null)) stamps[clip.GetInstanceID()] = ++clock;
        }

        static long Bytes(AudioClip clip)
        {
            return (long)clip.samples * clip.channels / 2;
        }

        sealed class Entry
        {
            public AudioClip clip;
            public int stamp;
            public long bytes;
        }

        // When a scene unloads (SceneUnloads).
        internal static void Release()
        {
            if (!MainThread.Current || !Active) return;
            float start = Time.realtimeSinceStartup;
            var inUse = new HashSet<int>();
            foreach (AudioSource source in Resources.FindObjectsOfTypeAll<AudioSource>())
            {
                if (source == null) continue;
                AudioClip clip = source.clip;
                if (clip != null && source.gameObject.activeInHierarchy) inUse.Add(clip.GetInstanceID());
            }
            // Stamps are kept only for clips whose data stays loaded (destroyed clips' ids go).
            var loaded = new List<Entry>();
            var keep = new List<Entry>();
            long total = 0;
            foreach (AudioClip clip in Resources.FindObjectsOfTypeAll<AudioClip>())
            {
                if (clip == null || clip.loadType == AudioClipLoadType.Streaming || clip.loadState != AudioDataLoadState.Loaded) continue;
                int id = clip.GetInstanceID(), stamp;
                if (!stamps.TryGetValue(id, out stamp)) stamp = ++clock;
                var entry = new Entry { clip = clip, stamp = stamp, bytes = Bytes(clip) };
                total += entry.bytes;
                (inUse.Contains(id) || stamp > lastUnload ? keep : loaded).Add(entry);
            }
            int kept = keep.Count;
            loaded.Sort((a, b) => a.stamp.CompareTo(b.stamp));
            int released = 0;
            long freed = 0;
            for (int i = 0; i < loaded.Count; i++)
            {
                Entry entry = loaded[i];
                if (total > budget && entry.clip.UnloadAudioData())
                {
                    total -= entry.bytes;
                    freed += entry.bytes;
                    released++;
                }
                else keep.Add(entry);
            }
            stamps.Clear();
            foreach (Entry entry in keep) stamps[entry.clip.GetInstanceID()] = entry.stamp;
            lastUnload = clock;
            if (unloads++ < 3 || released > 0)
                Debug.Log("AudioData: unloaded " + released + " clips (" + (freed / Megabyte).ToString("0.00") + " MB), " +
                          keep.Count + " stay loaded (" + (total / Megabyte).ToString("0.00") + " MB, " + kept + " in use or recent) in " +
                          ((Time.realtimeSinceStartup - start) * 1000f).ToString("0.0") + " ms");
        }
    }
}
