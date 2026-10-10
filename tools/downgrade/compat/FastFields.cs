using System;
using System.Collections.Generic;
using UnityEngine;

// Field access at the fields' offsets (FastFields) and the save surrogate's per-type plans (SaveSurrogate).
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // Field access without reflection, for PowerQuest's field copier (QuestUtils.CopyFields,
    // behind every room, character, item and their parts at each restore) and its save
    // surrogate. Through FieldInfo, IL2CPP fetches the field's Type object (under a lock),
    // checks generic arguments, boxes and converts for every value; here a field is copied
    // or set at its offset. Objects never move (Boehm GC) and need no write barrier.
    // Offsets come from UnsafeUtility.GetFieldOffset, calibrated once against a test class
    // (Ready). A field's bytes must lie after the object header and within its type's field
    // layout (Bounded). Until a field is verified, every access to it still goes through
    // FieldInfo, and its bytes in the object are compared with FieldInfo's value (references
    // by address, other values byte by byte; nothing found there is dereferenced). It is
    // verified once a value that is not null or all zero bytes has matched (a default value
    // would match at many wrong offsets too); only then is it read and written at its offset.
    // Many fields hold only defaults (null, 0, false), so before copying or restoring into an
    // object, an unverified field is first given a value of its own type that is not a default
    // (Probe: 1, true, a string, an empty array, an uninitialized instance of a concrete class,
    // engine classes included (a managed wrapper only, never used), a struct without
    // references with every byte 1), through FieldInfo, and compared; the real value is
    // written right after. Verification is
    // kept per field, for every path. A difference sends the field to FieldInfo for good.
    // Values whose type is not exactly the field's, Nullable fields, and other threads take
    // the FieldInfo path.
    // -compat-no-fast-fields turns it off.
    public static unsafe class FastFields
    {
        const System.Reflection.BindingFlags Declared = System.Reflection.BindingFlags.DeclaredOnly |
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;

        struct Holder { public object value; }
        sealed class Calibration { public long number; public object reference; }
        struct BoxCalibration { public int padding; public long number; }

        // Added to GetFieldOffset for fields of classes and of (boxed) structs; -1 not
        // calibrated, -2 failed.
        static int fieldDelta = -1, structDelta = -1, boxHeader = -1;

        static IntPtr Address(object value)
        {
            Holder holder;
            holder.value = value;
            return *(IntPtr*)Unity.Collections.LowLevel.Unsafe.UnsafeUtility.AddressOf(ref holder);
        }

        static object At(IntPtr address)
        {
            Holder holder;
            holder.value = null;
            *(IntPtr*)Unity.Collections.LowLevel.Unsafe.UnsafeUtility.AddressOf(ref holder) = address;
            return holder.value;
        }

        // 8 bytes at any alignment.
        static long ReadLong(byte* at)
        {
            long value;
            UnsafeCopy(&value, at, 8);
            return value;
        }

        public static bool Ready
        {
            get
            {
                if (fieldDelta == -1) Calibrate();
                return fieldDelta >= 0 && boxHeader >= 0;
            }
        }

        static void Calibrate()
        {
            fieldDelta = structDelta = boxHeader = -2;
            if (Platform.HasArg("-compat-no-fast-fields")) return;  // also the probe's flags (PS Vita)
            int delta = -2, header = -2, inStruct = -2;
            try
            {
                const long magic = 0x1122334455667788;
                var probe = new Calibration { number = magic };
                probe.reference = probe;
                byte* at = (byte*)Address(probe);
                int number = Unity.Collections.LowLevel.Unsafe.UnsafeUtility.GetFieldOffset(typeof(Calibration).GetField("number"));
                int reference = Unity.Collections.LowLevel.Unsafe.UnsafeUtility.GetFieldOffset(typeof(Calibration).GetField("reference"));
                foreach (int candidate in new[] { 0, 2 * IntPtr.Size })
                    if (ReadLong(at + number + candidate) == magic && *(IntPtr*)(at + reference + candidate) == (IntPtr)at) { delta = candidate; break; }
                object boxed = magic;
                byte* box = (byte*)Address(boxed);
                foreach (int candidate in new[] { 2 * IntPtr.Size, IntPtr.Size, 4 * IntPtr.Size })
                    if (ReadLong(box + candidate) == magic) { header = candidate; break; }
                object boxedStruct = new BoxCalibration { number = magic };
                byte* structBox = (byte*)Address(boxedStruct);
                int inner = Unity.Collections.LowLevel.Unsafe.UnsafeUtility.GetFieldOffset(typeof(BoxCalibration).GetField("number"));
                foreach (int candidate in new[] { 0, header })
                    if (candidate >= 0 && ReadLong(structBox + inner + candidate) == magic) { inStruct = candidate; break; }
            }
            catch (Exception) { delta = header = inStruct = -2; }
            if (delta >= 0 && header >= 0 && inStruct >= 0) { fieldDelta = delta; boxHeader = header; structDelta = inStruct; }
            Debug.Log("FastFields: " + (fieldDelta >= 0 ? "field offset delta " + fieldDelta + ", in boxed structs " + structDelta + ", box header " + boxHeader : "off"));
        }

        public sealed class Slot
        {
            public System.Reflection.FieldInfo field;
            public string name;
            public Type type, requested;
            public int offset, size;
            public bool reference, fast;
            public FieldState state;
            public TypeCode primitive;   // Empty unless a primitive type (not an enum)
        }

        // What is known about a field, shared by its slots.
        public sealed class FieldState { public bool verified, wrong; }

        static readonly Dictionary<long, FieldState> states = new Dictionary<long, FieldState>();

        static FieldState StateOf(System.Reflection.FieldInfo field)
        {
            long key = (long)field.FieldHandle.Value;
            FieldState state;
            if (!states.TryGetValue(key, out state)) states[key] = state = new FieldState();
            return state;
        }

        static int SizeOf(Type type)
        {
            if (type.IsEnum) type = Enum.GetUnderlyingType(type);
            if (type == typeof(bool) || type == typeof(byte) || type == typeof(sbyte)) return 1;
            if (type == typeof(char) || type == typeof(short) || type == typeof(ushort)) return 2;
            if (type == typeof(int) || type == typeof(uint) || type == typeof(float)) return 4;
            if (type == typeof(long) || type == typeof(ulong) || type == typeof(double)) return 8;
            if (type == typeof(IntPtr) || type == typeof(UIntPtr)) return IntPtr.Size;
            return Unity.Collections.LowLevel.Unsafe.UnsafeUtility.SizeOf(type);
        }

        public static Slot MakeSlot(System.Reflection.FieldInfo field, Type requested)
        {
            var slot = new Slot { field = field, name = field.Name, type = field.FieldType, requested = requested };
            try
            {
                slot.state = StateOf(field);
                slot.reference = !slot.type.IsValueType;
                slot.size = slot.reference ? IntPtr.Size : SizeOf(slot.type);
                slot.offset = Unity.Collections.LowLevel.Unsafe.UnsafeUtility.GetFieldOffset(field) +
                              (field.DeclaringType.IsValueType ? structDelta : fieldDelta);
                slot.primitive = slot.type.IsPrimitive && !slot.type.IsEnum ? Type.GetTypeCode(slot.type) : TypeCode.Empty;
                slot.fast = !field.IsStatic && !field.IsLiteral && !slot.type.IsPointer && slot.size > 0 &&
                            !(slot.type.IsGenericType && slot.type.GetGenericTypeDefinition() == typeof(Nullable<>)) &&
                            (!slot.reference || slot.offset % IntPtr.Size == 0) && Bounded(field, slot.offset, slot.size) &&
                            !slot.state.wrong;
            }
            catch (Exception)
            {
                slot.fast = false;
                if (slot.state == null) slot.state = new FieldState();
            }
            return slot;
        }

        // End of the instance fields' bytes of a class and its base classes, -1 if unknown.
        static readonly Dictionary<Type, int> extents = new Dictionary<Type, int>();

        static int Extent(Type type)
        {
            int end;
            if (extents.TryGetValue(type, out end)) return end;
            end = 0;
            try
            {
                for (Type at = type; at != null; at = at.BaseType)
                    foreach (System.Reflection.FieldInfo field in at.GetFields(Declared))
                    {
                        if (field.IsStatic || field.IsLiteral) continue;
                        int size = field.FieldType.IsValueType ? SizeOf(field.FieldType) : IntPtr.Size;
                        end = Math.Max(end, Unity.Collections.LowLevel.Unsafe.UnsafeUtility.GetFieldOffset(field) + fieldDelta + size);
                    }
            }
            catch (Exception) { end = -1; }
            extents[type] = end;
            return end;
        }

        // Whether [offset, offset + size) is past the object header (the same for boxes) and
        // within the declaring type's fields: a struct's size, or a class's Extent.
        static bool Bounded(System.Reflection.FieldInfo field, int offset, int size)
        {
            Type owner = field.DeclaringType;
            int end = owner.IsValueType ? boxHeader + Unity.Collections.LowLevel.Unsafe.UnsafeUtility.SizeOf(owner) : Extent(owner);
            return boxHeader > 0 && offset >= boxHeader && size > 0 && offset + size <= end;
        }

        // Whether the field's bytes at `at` hold `reflected`, its value as FieldInfo.GetValue
        // gives it; nonDefault: that value is not null or all zero bytes.
        static bool Matches(Slot slot, byte* at, object reflected, out bool nonDefault)
        {
            if (slot.reference)
            {
                IntPtr raw;
                UnsafeCopy(&raw, at, IntPtr.Size);
                nonDefault = reflected != null;
                return raw == (reflected == null ? IntPtr.Zero : Address(reflected));
            }
            nonDefault = false;
            if (reflected == null || reflected.GetType() != slot.type) return false;
            byte* value = (byte*)Address(reflected) + boxHeader;
            for (int i = 0; i < slot.size; i++)
            {
                if (at[i] != value[i]) return false;
                if (value[i] != 0) nonDefault = true;
            }
            return true;
        }

        // For a field not verified yet, after FieldInfo has read or set `reflected` in `owner`.
        static void Check(Slot slot, object owner, object reflected, string what)
        {
            bool nonDefault;
            if (Matches(slot, (byte*)Address(owner) + slot.offset, reflected, out nonDefault))
            {
                if (nonDefault) slot.state.verified = true;
                return;
            }
            slot.fast = false;
            slot.state.wrong = true;
            Debug.LogWarning("FastFields: " + slot.field.DeclaringType.FullName + "." + slot.name + " " + what + " differs from FieldInfo; using FieldInfo");
        }

        // Whether the slot can use its offset; false sends it to FieldInfo (and to Check).
        static bool Verified(Slot slot)
        {
            if (slot.state.verified) return true;
            if (slot.state.wrong) slot.fast = false;
            return false;
        }

        // Before an unverified field of `owner` is written through FieldInfo anyway: sets a
        // non-default value of its type through FieldInfo and compares (see above).
        static void Probe(Slot slot, object owner)
        {
            object value = ProbeValue(slot.type);
            if (value == null) return;
            try { slot.field.SetValue(owner, value); }
            catch (Exception) { return; }
            Check(slot, owner, slot.field.GetValue(owner), "probe");
        }

        static readonly Dictionary<Type, object> probes = new Dictionary<Type, object>();

        // A struct whose fields are all primitives, enums or such structs (no references).
        static bool Plain(Type type, int depth)
        {
            if (depth > 8) return false;
            foreach (System.Reflection.FieldInfo field in type.GetFields(Declared))
            {
                if (field.IsStatic) continue;
                Type inner = field.FieldType;
                if (inner.IsPointer || inner == typeof(IntPtr) || inner == typeof(UIntPtr)) return false;
                if (inner.IsPrimitive || inner.IsEnum) continue;
                if (!inner.IsValueType || !Plain(inner, depth + 1)) return false;
            }
            return true;
        }

        // A value of exactly this type that is not null or all zero bytes, or null if there is
        // none to make safely (interfaces, abstract classes, delegates, structs with references).
        static object ProbeValue(Type type)
        {
            object value;
            if (probes.TryGetValue(type, out value)) return value;
            value = null;
            try
            {
                if (type.IsEnum) value = Enum.ToObject(type, 1);
                else if (type.IsPrimitive)
                    switch (Type.GetTypeCode(type))
                    {
                        case TypeCode.Boolean: value = true; break;
                        case TypeCode.Char: value = (char)1; break;
                        case TypeCode.SByte: value = (sbyte)1; break;
                        case TypeCode.Byte: value = (byte)1; break;
                        case TypeCode.Int16: value = (short)1; break;
                        case TypeCode.UInt16: value = (ushort)1; break;
                        case TypeCode.Int32: value = 1; break;
                        case TypeCode.UInt32: value = 1u; break;
                        case TypeCode.Int64: value = 1L; break;
                        case TypeCode.UInt64: value = 1UL; break;
                        case TypeCode.Single: value = 1f; break;
                        case TypeCode.Double: value = 1.0; break;
                    }
                else if (type == typeof(string)) value = "FastFields";
                else if (type.IsArray && type.GetArrayRank() == 1) value = Array.CreateInstance(type.GetElementType(), 0);
                else if (type.IsClass && !type.IsAbstract && !type.IsArray && !type.ContainsGenericParameters && !type.IsCOMObject &&
                         !typeof(Delegate).IsAssignableFrom(type) && !typeof(System.Reflection.MemberInfo).IsAssignableFrom(type))
                {
                    value = System.Runtime.Serialization.FormatterServices.GetUninitializedObject(type);
                    GC.SuppressFinalize(value);
                }
                else if (type.IsValueType && !type.IsPrimitive && !type.ContainsGenericParameters && boxHeader > 0 && Plain(type, 0))
                {
                    value = Activator.CreateInstance(type);
                    byte* bytes = (byte*)Address(value) + boxHeader;
                    int size = SizeOf(type);
                    for (int i = 0; i < size; i++) bytes[i] = 1;
                }
            }
            catch (Exception) { value = null; }
            if (value != null && value.GetType() != type) value = null;
            probes[type] = value;
            return value;
        }

        static void UnsafeCopy(void* to, byte* from, int size)
        {
            Unity.Collections.LowLevel.Unsafe.UnsafeUtility.MemCpy(to, from, size);
        }

        static void Move(byte* from, byte* to, int size)
        {
            if (size == IntPtr.Size && ((long)to & (IntPtr.Size - 1)) == 0 && ((long)from & (IntPtr.Size - 1)) == 0) *(IntPtr*)to = *(IntPtr*)from;
            else if (size == 4 && ((long)to & 3) == 0 && ((long)from & 3) == 0) *(int*)to = *(int*)from;
            else if (size == 1) *to = *from;
            else Unity.Collections.LowLevel.Unsafe.UnsafeUtility.MemCpy(to, from, size);
        }

        // ---- QuestUtils.CopyFields(to, from): every declared instance field, same type only.
        static readonly Dictionary<Type, Slot[]> copies = new Dictionary<Type, Slot[]>();

        public static void Copy(object to, object from)
        {
            Type type = to.GetType();
            if (type != from.GetType()) return;
            Slot[] slots = null;
            if (MainThread.Current && Ready && !copies.TryGetValue(type, out slots))
            {
                System.Reflection.FieldInfo[] fields = ReflectionCache.GetFields(type, Declared);
                slots = new Slot[fields.Length];
                for (int i = 0; i < fields.Length; i++) slots[i] = MakeSlot(fields[i], fields[i].FieldType);
                copies[type] = slots;
            }
            if (slots == null)
            {
                foreach (System.Reflection.FieldInfo field in type.GetFields(Declared)) field.SetValue(to, field.GetValue(from));
                return;
            }
            byte* target = (byte*)Address(to), source = (byte*)Address(from);
            foreach (Slot slot in slots)
            {
                if (!slot.fast) { slot.field.SetValue(to, slot.field.GetValue(from)); continue; }
                if (!Verified(slot))
                {
                    object value = slot.field.GetValue(from);
                    Check(slot, from, value, "read");
                    if (slot.fast && !slot.state.verified) Probe(slot, to);
                    slot.field.SetValue(to, value);
                    continue;
                }
                Move(source + slot.offset, target + slot.offset, slot.size);
            }
        }

        // ---- Setting a field to a value as FieldInfo.SetValue would, when the value is exactly
        // of the field's type (or null for a reference field).
        public static void Set(Slot slot, object target, object value)
        {
            if (!slot.fast || (value == null ? !slot.reference : !slot.type.IsInstanceOfType(value)))
            {
                slot.field.SetValue(target, value);
                return;
            }
            if (!Verified(slot))
            {
                if (slot.fast) Probe(slot, target);
                slot.field.SetValue(target, value);
                if (slot.fast) Check(slot, target, slot.field.GetValue(target), "set");
                return;
            }
            byte* at = (byte*)Address(target) + slot.offset;
            if (slot.reference) *(IntPtr*)at = value == null ? IntPtr.Zero : Address(value);
            else Move((byte*)Address(value) + boxHeader, at, slot.size);
        }

        // ---- Reading a field as FieldInfo.GetValue would: reference fields and primitive
        // values (boxed here); other value types through FieldInfo.
        public static object Get(Slot slot, object target)
        {
            if (!slot.fast || (!slot.reference && slot.primitive == TypeCode.Empty)) return slot.field.GetValue(target);
            if (!Verified(slot))
            {
                object reflected = slot.field.GetValue(target);
                Check(slot, target, reflected, "read");
                return reflected;
            }
            byte* at = (byte*)Address(target) + slot.offset;
            object value;
            switch (slot.primitive)
            {
                case TypeCode.Empty: value = At(*(IntPtr*)at); break;
                case TypeCode.Boolean: value = *(bool*)at; break;
                case TypeCode.Byte: value = *at; break;
                case TypeCode.SByte: value = *(sbyte*)at; break;
                case TypeCode.Char: value = *(char*)at; break;
                case TypeCode.Int16: value = *(short*)at; break;
                case TypeCode.UInt16: value = *(ushort*)at; break;
                case TypeCode.Int32: value = *(int*)at; break;
                case TypeCode.UInt32: value = *(uint*)at; break;
                case TypeCode.Single: value = *(float*)at; break;
                case TypeCode.Int64: value = ReadLong(at); break;
                case TypeCode.UInt64: value = (ulong)ReadLong(at); break;
                case TypeCode.Double: value = BitConverter.Int64BitsToDouble(ReadLong(at)); break;
                default: return slot.field.GetValue(target);
            }
            return value;
        }
    }

    // PowerQuest's save surrogate (QuestSaveSurrogateSelector) decides for every object which of
    // its fields to save or restore: the type's QuestSave attribute, each field's attributes and
    // the surrogate's own type tests, all of which depend only on the type. Here that decision
    // is made once per type, with the surrogate's own methods, and kept as a list of fields;
    // each object then only moves its values (FastFields). The downgrader starts SetObjectData
    // and GetObjectData with these Try methods, which return false where the original code
    // must run: other threads, Vector2 and Color.
    public static class SaveSurrogate
    {
        public sealed class Plan
        {
            public bool original, ignored, cachable;
            public FastFields.Slot[] slots;
        }

        static readonly Dictionary<Type, Plan> restorePlans = new Dictionary<Type, Plan>(), savePlans = new Dictionary<Type, Plan>();
        static System.Reflection.MethodInfo isIgnored, isKnown, isNullable, firstArgument;
        static Type questSave, questDontSave, cachable;
        static System.Reflection.BindingFlags flags;
        static bool prepared, usable;

        static bool Prepare(object selector)
        {
            if (prepared) return usable;
            prepared = true;
            Type type = selector.GetType();
            var all = System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Instance |
                      System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
            System.Reflection.FieldInfo flagsField = type.GetField("BINDING_FLAGS", all), saveField = type.GetField("TYPE_QUESTSAVE", all),
                dontSaveField = type.GetField("TYPE_QUESTDONTSAVE", all);
            isIgnored = type.GetMethod("IsIgnoredType", all);
            isKnown = type.GetMethod("IsKnownType", all);
            isNullable = type.GetMethod("IsNullableType", all);
            firstArgument = type.GetMethod("GetFirstArgumentOfGenericType", all);
            cachable = type.Assembly.GetType("PowerTools.Quest.IQuestSaveCachable");
            usable = flagsField != null && saveField != null && dontSaveField != null && isIgnored != null && isKnown != null &&
                     isNullable != null && firstArgument != null && cachable != null;
            if (!usable) { Debug.LogWarning("SaveSurrogate: surrogate members not found; using the original code"); return false; }
            flags = (System.Reflection.BindingFlags)flagsField.GetValue(null);
            questSave = (Type)saveField.GetValue(null);
            questDontSave = (Type)dontSaveField.GetValue(null);
            return true;
        }

        static bool Ask(System.Reflection.MethodInfo method, object selector, Type type)
        {
            return (bool)method.Invoke(method.IsStatic ? null : selector, new object[] { type });
        }

        static Plan PlanFor(object selector, Type type, bool saving)
        {
            Dictionary<Type, Plan> plans = saving ? savePlans : restorePlans;
            Plan plan;
            if (plans.TryGetValue(type, out plan)) return plan;
            plan = new Plan();
            plans[type] = plan;
            if (type == typeof(Vector2) || type == typeof(Color)) { plan.original = true; return plan; }
            plan.cachable = saving && cachable.IsAssignableFrom(type);
            bool markedFieldsOnly = ReflectionCache.IsDefined(type, questSave);
            if (Ask(isIgnored, selector, type) && !markedFieldsOnly) { plan.ignored = true; return plan; }
            var slots = new List<FastFields.Slot>();
            foreach (System.Reflection.FieldInfo field in ReflectionCache.GetFields(type, flags))
            {
                try
                {
                    Type fieldType = field.FieldType;
                    if ((markedFieldsOnly && !ReflectionCache.IsDefined(field, questSave)) || Ask(isIgnored, selector, fieldType)) continue;
                    if (saving && ReflectionCache.IsDefined(field, questDontSave)) continue;
                    Type requested;
                    if (Ask(isKnown, selector, fieldType))
                    {
                        if (saving && field.Name.Length > 0 && field.Name[0] == '$') break;
                        requested = !saving && Ask(isNullable, selector, fieldType)
                            ? (Type)firstArgument.Invoke(selector, new object[] { fieldType }) : fieldType;
                    }
                    else if (fieldType.IsClass || fieldType.IsValueType) requested = fieldType;
                    else continue;
                    slots.Add(FastFields.MakeSlot(field, requested));
                }
                catch (Exception e) { Failed("planning", type, field.Name, e); }
            }
            plan.slots = slots.ToArray();
            return plan;
        }

        public static bool TrySetObjectData(object selector, object obj, System.Runtime.Serialization.SerializationInfo info, out object result)
        {
            result = obj;
            if (!MainThread.Current || !FastFields.Ready || !Prepare(selector)) return false;
            try
            {
                Type type = obj.GetType();
                if (info.MemberCount <= 0) return true;
                Plan plan = PlanFor(selector, type, false);
                if (plan.original) return false;
                if (plan.ignored) return true;
                foreach (FastFields.Slot slot in plan.slots)
                {
                    try
                    {
                        object value = SaveFields.GetValue(info, slot.name, slot.requested);
                        if (value != SaveFields.Missing) FastFields.Set(slot, obj, value);
                    }
                    catch (Exception e) { Failed("restoring", type, slot.name, e); }
                }
            }
            catch (Exception e) { Failed("restoring", obj == null ? null : obj.GetType(), null, e); }
            return true;
        }

        // unchangedCachable: obj is an IQuestSaveCachable whose SaveDirtyEver is false, which
        // the original skips (the downgrader computes it before the call).
        public static bool TryGetObjectData(object selector, object obj, System.Runtime.Serialization.SerializationInfo info, bool unchangedCachable)
        {
            if (obj == null || !MainThread.Current || !FastFields.Ready || !Prepare(selector)) return false;
            try
            {
                Plan plan = PlanFor(selector, obj.GetType(), true);
                if (plan.original) return false;
                if (plan.cachable && unchangedCachable) return true;
                if (plan.ignored) return true;
                foreach (FastFields.Slot slot in plan.slots) info.AddValue(slot.name, FastFields.Get(slot, obj));
            }
            catch (Exception e) { Failed("saving", obj.GetType(), null, e); }
            return true;
        }

        // The game's surrogate skips whatever throws without a word (a field, or the rest of an
        // object), and so do these; the first failures are logged, so a save or restore that
        // lost data says so.
        const int LoggedFailures = 20;
        static int failures;

        static void Failed(string what, Type type, string field, Exception e)
        {
            if (++failures > LoggedFailures) return;
            Debug.LogWarning("SaveSurrogate: " + what + " " + (type == null ? "null" : type.FullName) + (field == null ? "" : "." + field) + " failed: " +
                             e.GetType().Name + " " + e.Message + (failures == LoggedFailures ? " (later failures are not logged)" : ""));
        }
    }
}
