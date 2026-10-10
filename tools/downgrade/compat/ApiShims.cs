using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.U2D;
using Object = UnityEngine.Object;

// Unity 2020 types and members that 2018.2 lacks, by the naming convention in Compat2018.cs.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Platforms without a video player (PS Vita's VideoModule has no types). The game
    // waits until a started video plays and then while it plays; this one reports
    // playing for the two frames after Play, so those waits finish as if it had ended.
    public class UnityEngine_Video_VideoPlayer : MonoBehaviour
    {
        int playedFrame = -10;

        public bool isPlaying { get { return Time.frameCount <= playedFrame + 1; } }
        public void Play() { playedFrame = Time.frameCount; }
        public void Stop() { playedFrame = -10; }
        public void Prepare() { }
        public UnityEngine_Video_VideoClip clip { get; set; }
        public Camera targetCamera { get; set; }
        public bool playOnAwake { get; set; }
        public bool waitForFirstFrame { get; set; }
        public bool isLooping { get; set; }
        public bool skipOnDrop { get; set; }
        public UnityEngine_Video_VideoRenderMode renderMode { get; set; }
        public UnityEngine_Video_Video3DLayout targetCamera3DLayout { get; set; }
        public UnityEngine_Video_VideoAspectRatio aspectRatio { get; set; }
        public UnityEngine_Video_VideoAudioOutputMode audioOutputMode { get; set; }
        public void SetDirectAudioVolume(ushort trackIndex, float volume) { }
    }

    public class UnityEngine_Video_VideoClip : ScriptableObject { }
    public enum UnityEngine_Video_VideoRenderMode { CameraFarPlane = 0, CameraNearPlane = 1, RenderTexture = 2, MaterialOverride = 3, APIOnly = 4 }
    public enum UnityEngine_Video_Video3DLayout { No3D = 0, SideBySide3D = 1, OverUnder3D = 2 }
    public enum UnityEngine_Video_VideoAspectRatio { NoScaling = 0, FitVertically = 1, FitHorizontally = 2, FitInside = 3, FitOutside = 4, Stretch = 5 }
    public enum UnityEngine_Video_VideoAudioOutputMode { None = 0, AudioSource = 1, Direct = 2, APIOnly = 3 }

    public enum UnityEngine_FindObjectsInactive { Exclude = 0, Include = 1 }
    public enum UnityEngine_FindObjectsSortMode { None = 0, InstanceID = 1 }
    public enum UnityEngine_Rendering_SinglePassStereoMode { None = 0, SideBySide = 1, Instancing = 2, Multiview = 3 }

    public static class UnityEngine_Object
    {
        public static T FindFirstObjectByType<T>() where T : Object
        {
            return Object.FindObjectOfType<T>();
        }

        public static T[] FindObjectsOfType<T>(bool includeInactive) where T : Object
        {
            return includeInactive ? SceneObjects<T>() : Object.FindObjectsOfType<T>();
        }

        public static T[] FindObjectsByType<T>(UnityEngine_FindObjectsInactive inactive, UnityEngine_FindObjectsSortMode sort) where T : Object
        {
            T[] found = FindObjectsOfType<T>(inactive == UnityEngine_FindObjectsInactive.Include);
            if (sort == UnityEngine_FindObjectsSortMode.InstanceID)
                Array.Sort(found, (a, b) => a.GetInstanceID().CompareTo(b.GetInstanceID()));
            return found;
        }

        // FindObjectsOfTypeAll also returns assets and hidden objects; keep loaded scene objects.
        static T[] SceneObjects<T>() where T : Object
        {
            return Resources.FindObjectsOfTypeAll<T>().Where(o =>
            {
                if ((o.hideFlags & (HideFlags.NotEditable | HideFlags.HideAndDontSave)) != 0) return false;
                var component = o as Component;
                var gameObject = component != null ? component.gameObject : o as GameObject;
                return gameObject == null || gameObject.scene.IsValid();
            }).ToArray();
        }
    }

    public static class UnityEngine_RenderTexture
    {
        public static RenderTexture ctor(int width, int height, int depth, RenderTextureFormat format, int mipCount)
        {
            var texture = new RenderTexture(width, height, depth, format);
            if (mipCount > 1) texture.useMipMap = true;
            return texture;
        }
    }

    public static class UnityEngine_RenderTextureDescriptor
    {
        public static void set_mipCount(ref RenderTextureDescriptor descriptor, int value)
        {
            descriptor.useMipMap = value > 1;
        }
    }

    public static class UnityEngine_Texture
    {
        // Unity 2018 has no readable flag; reading a non-readable texture throws.
        public static bool get_isReadable(Texture texture)
        {
            var texture2D = texture as Texture2D;
            if (texture2D == null) return false;
            try { texture2D.GetPixel(0, 0); return true; }
            catch (UnityException) { return false; }
        }
    }

    public static class UnityEngine_ParticleSystem
    {
        public static void SetParticles(ParticleSystem system, ParticleSystem.Particle[] particles, int size, int offset)
        {
            if (offset == 0) { system.SetParticles(particles, size); return; }
            var current = new ParticleSystem.Particle[Math.Max(system.particleCount, offset + size)];
            int count = system.GetParticles(current);
            Array.Copy(particles, 0, current, offset, size);
            system.SetParticles(current, Math.Max(count, offset + size));
        }
    }

    public static class UnityEngine_U2D_SpriteAtlasManager
    {
        static readonly Dictionary<Action<string, Action<SpriteAtlas>>, SpriteAtlasManager.RequestAtlasCallback> callbacks =
            new Dictionary<Action<string, Action<SpriteAtlas>>, SpriteAtlasManager.RequestAtlasCallback>();

        public static void add_atlasRequested(Action<string, Action<SpriteAtlas>> handler)
        {
            if (handler == null || callbacks.ContainsKey(handler)) return;
            SpriteAtlasManager.RequestAtlasCallback callback = (tag, action) => handler(tag, action);
            callbacks[handler] = callback;
            SpriteAtlasManager.atlasRequested += callback;
        }

        public static void remove_atlasRequested(Action<string, Action<SpriteAtlas>> handler)
        {
            SpriteAtlasManager.RequestAtlasCallback callback;
            if (handler == null || !callbacks.TryGetValue(handler, out callback)) return;
            SpriteAtlasManager.atlasRequested -= callback;
            callbacks.Remove(handler);
        }
    }

    public static class UnityEngine_Experimental_Rendering_GraphicsFormatUtility
    {
        public static GraphicsFormat GetGraphicsFormat(RenderTextureFormat format, RenderTextureReadWrite readWrite)
        {
            bool sRGB = readWrite == RenderTextureReadWrite.sRGB ||
                (readWrite == RenderTextureReadWrite.Default && QualitySettings.activeColorSpace == ColorSpace.Linear);
            return GraphicsFormatUtility.GetGraphicsFormat(format, sRGB);
        }
    }

    public static class UnityEngine_Rendering_CommandBuffer
    {
        public static void SetSinglePassStereo(CommandBuffer buffer, UnityEngine_Rendering_SinglePassStereoMode mode) { }
    }

    public static class UnityEngine_Rendering_GraphicsSettings
    {
        public static RenderPipelineAsset get_currentRenderPipeline()
        {
            return GraphicsSettings.renderPipelineAsset;
        }
    }

    public static class UnityEngine_SystemInfo
    {
        public static bool get_usesLoadStoreActions() { return false; }
    }
}
