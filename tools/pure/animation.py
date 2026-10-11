"""Bake an .anim's editor curves into the runtime clip the player reads (AnimationClip, class 74).

The player never reads the editor curve arrays of a Mecanim (non-legacy) clip: the build writes
them empty and stores the clip as a muscle clip, which the editor bakes from the curves at import:

- Every curve gets a GenericBinding: CRC32 of its transform path and of its property name, the
  class it animates (typeID), and for properties the engine binds itself a small attribute
  number and a customType instead of a hash (Transform position 1, rotation 2, scale 3, euler 4;
  SpriteRenderer.m_Sprite 0 with customType 23). A vector curve (position, scale, euler) is one
  binding over 3 float curves.
- Each binding goes to the streamed, dense or constant clip by key count, not by value: a float
  binding whose curves have a single key is constant; every other one, and every PPtr curve (even
  one key, even one sprite throughout), is streamed. No reference clip has dense curves, so none
  are made. Bindings are ordered streamed, then dense, then constant, each kind in the order the
  curves are added: position, rotation, euler, scale, float, PPtr. (The project has no rotation
  curves; their 4-component binding is untested.)
- A streamed curve is a list of cubic segments keyed by start time. Frames group the keys of
  all curves by time: a frame at -FLT_MAX holds each curve's value before its first key, then
  every key's segment to the next key (a constant for the last key), then an empty frame at +inf.
  A PPtr curve is a stepped float curve of indices into pptrCurveMapping; its first key is the
  -FLT_MAX one.
- Coefficients are computed as the editor's AnimationCurve cache does, in float32, so they come
  out bit-equal. m_ValueArrayDelta holds each curve's value at the clip's start and stop times.
- m_MuscleClipSize (the runtime allocation) and the dense clip's frame count are fitted on the
  reference; a clip without curves gets no runtime clip (size 0, empty Clip).

The bindings' script PPtrs name MonoScripts, so ctx.pptr must resolve script references.

The same goes for AnimatorController (class 91): the editor bakes the state machines into
m_Controller; see controller().
"""

import struct
import zlib
from dataclasses import dataclass, field

import convert
import numpy as np

F = np.float32
FLT_MAX = float(np.finfo(np.float32).max)
INF = float('inf')

STREAMED, CONSTANT = range(2)
# Transform properties the engine binds itself: (YAML array, attribute, customType, components).
TRANSFORM_CURVES = (
    ('m_PositionCurves', 1, 0, 'xyz'),
    ('m_RotationCurves', 2, 0, 'xyzw'),
    ('m_EulerCurves', 4, 4, 'xyz'),
    ('m_ScaleCurves', 3, 0, 'xyz'),
)
# (classID, property) bound by the engine rather than by a property hash: (attribute, customType).
CUSTOM_BINDINGS = {(212, 'm_Sprite'): (0, 23)}
# The editor-only curve arrays the build leaves empty.
EDITOR_CURVES = (
    'm_RotationCurves', 'm_CompressedRotationCurves', 'm_EulerCurves', 'm_PositionCurves', 'm_ScaleCurves',
    'm_FloatCurves', 'm_PPtrCurves',
)  # fmt: skip
# Bytes of the runtime clip the player allocates (ClipMuscleConstant and its fixed-size human pose)
# before the curve data, which adds 4 per streamed word and constant and 8 per curve (its value
# delta); fitted on all 3176 reference clips with curves.
MUSCLE_CLIP_BASE = 2544


def f32(value):
    return float(F(value))


def crc32(text):
    return zlib.crc32(text.encode('utf-8'))


@dataclass
class Key:
    time: float
    value: float
    in_slope: float = 0.0
    out_slope: float = 0.0


@dataclass
class Binding:
    path: int
    attribute: int
    script: dict
    type_id: int
    custom_type: int
    pptr: bool
    curves: list = field(default_factory=list)  # one list of Keys per component

    @property
    def kind(self):
        if not self.pptr and all(len(c) == 1 for c in self.curves):
            return CONSTANT
        return STREAMED


def number(text):
    return f32(convert.number(text, 'float'))


def flag(fields, name, default='0'):
    return fields.get(name, default) not in ('0', '')


def float_keys(curve, component=None):
    keys = []
    for k in curve['m_Curve'] or []:

        def pick(v):
            return number(v[component] if component else v)

        keys.append(Key(number(k['time']), pick(k['value']), pick(k['inSlope']), pick(k['outSlope'])))
    return keys


def bindings(fields, ctx):
    """The clip's bindings in the order the editor adds them, and the PPtr curves' objects."""
    out = []
    for name, attribute, custom, components in TRANSFORM_CURVES:
        for c in fields.get(name) or []:
            b = Binding(crc32(c.get('path') or ''), attribute, {'m_FileID': 0, 'm_PathID': 0}, 4, custom, False)
            b.curves = [float_keys(c['curve'], axis) for axis in components]
            out.append(b)
    for c in fields.get('m_FloatCurves') or []:
        class_id = int(c['classID'])
        attribute, custom = CUSTOM_BINDINGS.get((class_id, c['attribute']), (crc32(c['attribute']), 0))
        b = Binding(crc32(c.get('path') or ''), attribute, ctx.pptr(c['script'], None), class_id, custom, False)
        b.curves = [float_keys(c['curve'])]
        out.append(b)
    mapping = []
    for c in fields.get('m_PPtrCurves') or []:
        class_id = int(c['classID'])
        attribute, custom = CUSTOM_BINDINGS.get((class_id, c['attribute']), (crc32(c['attribute']), 0))
        b = Binding(crc32(c.get('path') or ''), attribute, ctx.pptr(c['script'], None), class_id, custom, True)
        keys = []
        for k in c['curve'] or []:
            keys.append(Key(number(k['time']), float(len(mapping)), INF, INF))
            mapping.append(ctx.pptr(k['value'], None))
        b.curves = [keys]
        out.append(b)
    return sorted(out, key=lambda b: b.kind), mapping


def segment(lhs, rhs):
    """The cubic (a, b, c, d) of the curve between two keys, as AnimationCurve's cache computes it."""
    if lhs.out_slope == INF or rhs.in_slope == INF:
        return (0.0, 0.0, 0.0, lhs.value)
    dx = F(rhs.time) - F(lhs.time)
    dx = max(dx, F(0.0001))
    dy = F(rhs.value) - F(lhs.value)
    length = F(1.0) / (dx * dx)
    m1, m2 = F(lhs.out_slope), F(rhs.in_slope)
    d1, d2 = m1 * dx, m2 * dx
    a = (d1 + d2 - dy - dy) * length / dx
    b = (dy + dy + dy - d1 - d1 - d2) * length
    return (float(a), float(b), float(m1), lhs.value)


def streamed_keys(keys, pptr):
    """[(time, coefficients)] of one curve."""
    out = [(-FLT_MAX, (0.0, 0.0, 0.0, keys[0].value))]
    for i, k in enumerate(keys):
        if pptr and i == 0:
            continue
        out.append((k.time, segment(k, keys[i + 1]) if i + 1 < len(keys) else (0.0, 0.0, 0.0, k.value)))
    return out


def streamed_clip(curves):
    """The StreamedClip words for [(keys, is PPtr)]."""
    frames = {}
    for index, (keys, pptr) in enumerate(curves):
        for time, coeff in streamed_keys(keys, pptr):
            frames.setdefault(time, []).append((index, coeff))
    frames[INF] = []
    words = []
    for time in sorted(frames):
        words += [as_word(time), len(frames[time])]
        for index, coeff in frames[time]:
            words += [index, *map(as_word, coeff)]
    return words


def as_word(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


def evaluate(keys, pptr, time):
    """The value of a baked curve at a time, as the player evaluates the streamed clip."""
    current = None
    for t, coeff in streamed_keys(keys, pptr):
        if t <= time:
            current = (t, coeff)
    t, (a, b, c, d) = current
    x = F(time) - F(t)
    return float(((F(a) * x + F(b)) * x + F(c)) * x + F(d))


def identity_xform():
    return {
        't': {'x': 0.0, 'y': 0.0, 'z': 0.0},
        'q': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0},
        's': {'x': 1.0, 'y': 1.0, 'z': 1.0},
    }


def zero3():
    return {'x': 0.0, 'y': 0.0, 'z': 0.0}


def hand_pose():
    return {
        'm_GrabX': identity_xform(),
        'm_DoFArray': [0.0] * 20,
        'm_Override': 0.0,
        'm_CloseOpen': 0.0,
        'm_InOut': 0.0,
        'm_Grab': 0.0,
    }


def human_pose():
    goal = {'m_WeightT': 0.0, 'm_WeightR': 0.0, 'm_HintT': zero3(), 'm_HintWeightT': 0.0}
    return {
        'm_RootX': identity_xform(),
        'm_LookAtPosition': zero3(),
        'm_LookAtWeight': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 0.0},
        'm_GoalArray': [{'m_X': identity_xform(), **goal} for _ in range(4)],
        'm_LeftHandPose': hand_pose(),
        'm_RightHandPose': hand_pose(),
        'm_DoFArray': [0.0] * 55,
        'm_TDoFArray': [zero3() for _ in range(21)],
    }


def dense_frame_count(start, stop, rate):
    # Written even with no dense curves; fitted on all 3176 reference clips that have curves.
    return int(np.floor(F(F(stop) - F(start)) * F(rate))) + 2


def muscle_clip(fields, ordered):
    settings = fields['m_AnimationClipSettings']
    rate = number(fields['m_SampleRate'])
    start, stop = number(settings['m_StartTime']), number(settings['m_StopTime'])
    curves = [(keys, b.pptr, b.kind) for b in ordered for keys in b.curves]
    streamed = [(keys, pptr) for keys, pptr, kind in curves if kind == STREAMED]
    constant = [keys[0].value for keys, pptr, kind in curves if kind == CONSTANT]
    words = streamed_clip(streamed) if curves else []
    delta = [{'m_Start': evaluate(k, p, start), 'm_Stop': evaluate(k, p, stop)} for k, p, _ in curves]

    clip = {
        'm_DeltaPose': human_pose(),
        'm_StartX': identity_xform(),
        'm_StopX': identity_xform(),
        'm_LeftFootStartX': identity_xform(),
        'm_RightFootStartX': identity_xform(),
        'm_AverageSpeed': zero3(),
        'm_Clip': {
            'data': {
                'm_StreamedClip': {'data': words, 'curveCount': len(streamed)},
                'm_DenseClip': {
                    'm_FrameCount': dense_frame_count(start, stop, rate) if curves else 0,
                    'm_CurveCount': 0,
                    'm_SampleRate': rate if curves else 0.0,
                    'm_BeginTime': start,
                    'm_SampleArray': [],
                },
                'm_ConstantClip': {'data': constant},
                'm_Binding': {'data': {'m_ValueArray': []}},
            }
        },
        'm_StartTime': start,
        'm_StopTime': stop,
        'm_OrientationOffsetY': number(settings.get('m_OrientationOffsetY', '0')),
        'm_Level': number(settings.get('m_Level', '0')),
        'm_CycleOffset': number(settings.get('m_CycleOffset', '0')),
        'm_AverageAngularSpeed': 0.0,
        'm_IndexArray': [-1] * 200,
        'm_ValueArrayDelta': delta,
        'm_ValueArrayReferencePose': [],
        'm_Mirror': flag(settings, 'm_Mirror'),
        'm_LoopTime': flag(settings, 'm_LoopTime'),
        'm_LoopBlend': flag(settings, 'm_LoopBlend'),
        'm_LoopBlendOrientation': flag(settings, 'm_LoopBlendOrientation'),
        'm_LoopBlendPositionY': flag(settings, 'm_LoopBlendPositionY'),
        'm_LoopBlendPositionXZ': flag(settings, 'm_LoopBlendPositionXZ'),
        'm_StartAtOrigin': True,  # no root motion in a generic clip
        'm_KeepOriginalOrientation': flag(settings, 'm_KeepOriginalOrientation'),
        'm_KeepOriginalPositionY': flag(settings, 'm_KeepOriginalPositionY'),
        'm_KeepOriginalPositionXZ': flag(settings, 'm_KeepOriginalPositionXZ'),
        'm_HeightFromFeet': flag(settings, 'm_HeightFromFeet'),
    }
    size = MUSCLE_CLIP_BASE + 4 * (len(words) + len(constant)) + 8 * len(delta) if curves else 0
    return clip, size


def clip(doc, ctx):
    """The release AnimationClip dict (UnityPy type tree form) for an .anim YAML document."""
    fields = dict(doc.fields)
    for name in EDITOR_CURVES:
        fields[name] = []
    for name in ('m_MuscleClipSize', 'm_MuscleClip', 'm_ClipBindingConstant'):
        fields.pop(name, None)
    out, _ = convert.convert(74, fields, ctx)
    ordered, mapping = bindings(doc.fields, ctx)
    out['m_MuscleClip'], out['m_MuscleClipSize'] = muscle_clip(doc.fields, ordered)
    out['m_ClipBindingConstant'] = {
        'genericBindings': [
            {
                'path': b.path,
                'attribute': b.attribute,
                'script': b.script,
                'typeID': b.type_id,
                'customType': b.custom_type,
                'isPPtrCurve': int(b.pptr),
            }
            for b in ordered
        ],
        'pptrCurveMapping': mapping,
    }
    return out


# Runtime controller sizes, fitted on the reference's 5 controllers (one layer, one state machine,
# no parameters or transitions): the controller with its layer, state machine and the entry and
# exit selector states; each state; each state's blend tree of one clip.
CONTROLLER_BASE, STATE_SIZE, CLIP_TREE_SIZE = 592, 144, 152
# The editor adds this parameter name to every controller's string table.
GRAVITY_WEIGHT = 'GravityWeight'
# The exit selector's transition leaves the state machine.
EXIT_DESTINATION = 30000


def unsupported(what):
    raise NotImplementedError(f'AnimatorController with {what}: not baked yet (none in this project)')


def clip_node(clip_id):
    return {
        'data': {
            'm_BlendType': 0,
            'm_BlendEventID': 0xFFFFFFFF,
            'm_BlendEventYID': 0xFFFFFFFF,
            'm_ChildIndices': [],
            'm_Blend1dData': {'data': {'m_ChildThresholdArray': []}},
            'm_Blend2dData': {
                'data': {
                    'm_ChildPositionArray': [],
                    'm_ChildMagnitudeArray': [],
                    'm_ChildPairVectorArray': [],
                    'm_ChildPairAvgMagInvArray': [],
                    'm_ChildNeighborListArray': [],
                }
            },
            'm_BlendDirectData': {'data': {'m_ChildBlendEventIDArray': [], 'm_NormalizedBlendValues': False}},
            'm_ClipID': clip_id,
            'm_Duration': 1.0,
            'm_CycleOffset': 0.0,
            'm_Mirror': False,
        }
    }


def selector(full_path_id, destination, entry):
    return {
        'data': {
            'm_TransitionConstantArray': [{'data': {'m_Destination': destination, 'm_ConditionConstantArray': []}}],
            'm_FullPathID': full_path_id,
            'm_IsEntry': entry,
        }
    }


def check_supported(layers, machine, states):
    if len(layers) != 1:
        unsupported(f'{len(layers)} layers')
    layer = layers[0]
    if int(layer['m_Mask'].get('fileID', 0)) or layer.get('m_Motions') or layer.get('m_Behaviours'):
        unsupported('a layer mask, synced motions or layer behaviours')
    for name in ('m_ChildStateMachines', 'm_AnyStateTransitions', 'm_EntryTransitions', 'm_StateMachineBehaviours'):
        if machine.get(name):
            unsupported(name)
    if machine.get('m_StateMachineTransitions') not in (None, {}, []):
        unsupported('state machine transitions')
    for state in states:
        if state.get('m_Transitions') or state.get('m_StateMachineBehaviours') or state.get('m_Tag'):
            unsupported('state transitions, behaviours or tags')
        parameters = ('m_SpeedParameterActive', 'm_MirrorParameterActive', 'm_CycleOffsetParameterActive')
        if any(flag(state, name) for name in (*parameters, 'm_TimeParameterActive')):
            unsupported('state parameters')


def state_constant(state, full_name, loop, trees):
    return {
        'data': {
            'm_TransitionConstantArray': [],
            'm_BlendTreeConstantIndexArray': [0 if trees else -1],
            'm_BlendTreeConstantArray': trees,
            'm_NameID': crc32(state['m_Name']),
            'm_PathID': crc32(full_name),
            'm_FullPathID': crc32(full_name),
            'm_TagID': 0,
            'm_SpeedParamID': 0,
            'm_MirrorParamID': 0,
            'm_CycleOffsetParamID': 0,
            'm_TimeParamID': 0,
            'm_Speed': number(state.get('m_Speed', '1')),
            'm_CycleOffset': number(state.get('m_CycleOffset', '0')),
            'm_IKOnFeet': flag(state, 'm_IKOnFeet'),
            'm_WriteDefaultValues': flag(state, 'm_WriteDefaultValues', '1'),
            'm_Loop': loop,
            'm_Mirror': flag(state, 'm_Mirror'),
        }
    }


def layer_constant(layer):
    return {
        'data': {
            'm_StateMachineIndex': 0,
            'm_StateMachineSynchronizedLayerIndex': 0,
            # every humanoid body part (19 bits in the last word) on: no mask
            'm_BodyMask': {'word0': 0xFFFFFFFF, 'word1': 0xFFFFFFFF, 'word2': 0x7FFFF},
            'm_SkeletonMask': {'data': {'m_Data': []}},
            'm_Binding': crc32(layer['m_Name']),
            '(int&)m_LayerBlendingMode': int(layer.get('m_BlendingMode', 0)),
            'm_DefaultWeight': number(layer.get('m_DefaultWeight', '0')),
            'm_IKPass': flag(layer, 'm_IKPass'),
            'm_SyncedLayerAffectsTiming': flag(layer, 'm_SyncedLayerAffectsTiming'),
        }
    }


def controller(doc, ctx, asset, db):
    """The release AnimatorController dict for a .controller's controller document. asset is the
    .controller's Asset (its state machine and states are its other documents), db the AssetDB
    (a state loops if its clip does).

    The editor bakes the editor objects (AnimatorStateMachine, AnimatorState, ...; never written to
    the player) into m_Controller, names everything by CRC32 and lists the names in m_TOS. Only the
    shape this project uses is baked: one layer, one state machine, states that play a clip or
    nothing, no parameters or transitions; anything else raises NotImplementedError."""
    fields = doc.fields
    if fields.get('m_AnimatorParameters'):
        unsupported('parameters')
    layers = fields.get('m_AnimatorLayers') or []
    layer = layers[0] if layers else None
    machine = asset.docs[int(layer['m_StateMachine']['fileID'])].fields if layer else {}
    child_ids = [int(c['m_State']['fileID']) for c in machine.get('m_ChildStates') or []]
    check_supported(layers, machine, [asset.docs[i].fields for i in child_ids])

    layer_name = layer['m_Name']
    names = {0: '', crc32(GRAVITY_WEIGHT): GRAVITY_WEIGHT, crc32(layer_name): layer_name}
    clips, clip_keys, states = [], [], []
    for state_id in child_ids:
        state = asset.docs[state_id].fields
        full_name = f'{layer_name}.{state["m_Name"]}'
        names[crc32(state['m_Name'])] = state['m_Name']
        names[crc32(full_name)] = full_name
        motion = state['m_Motion']
        loop, trees = True, []
        if int(motion.get('fileID', 0)):
            target = db.by_guid[motion['guid']].docs[int(motion['fileID'])]
            if target.class_id != 74:
                unsupported(f'a motion of class {target.class_id}')
            loop = flag(target.fields['m_AnimationClipSettings'], 'm_LoopTime')
            pptr = ctx.pptr(motion, None)
            key = (pptr.get('file'), pptr['m_PathID'])
            if key not in clip_keys:
                clips.append(pptr)
                clip_keys.append(key)
            trees = [{'data': {'m_NodeArray': [clip_node(clip_keys.index(key))]}}]
        states.append(state_constant(state, full_name, loop, trees))

    machine_id = crc32(machine.get('m_Name', layer_name))
    default = int(machine['m_DefaultState'].get('fileID', 0))
    default_index = child_ids.index(default) if default in child_ids else 0
    baked = {
        'm_LayerArray': [layer_constant(layer)],
        'm_StateMachineArray': [
            {
                'data': {
                    'm_StateConstantArray': states,
                    'm_AnyStateTransitionConstantArray': [],
                    'm_SelectorStateConstantArray': [
                        selector(machine_id, default_index, True),
                        selector(machine_id, EXIT_DESTINATION, False),
                    ],
                    'm_DefaultState': default_index,
                    'm_SynchronizedLayerCount': 1,
                }
            }
        ],
        'm_Values': {'data': {'m_ValueArray': []}},
        'm_DefaultValues': {
            'data': {
                'm_PositionValues': [],
                'm_QuaternionValues': [],
                'm_ScaleValues': [],
                'm_FloatValues': [],
                'm_IntValues': [],
                'm_BoolValues': [],
            }
        },
    }
    trees = sum(len(s['data']['m_BlendTreeConstantArray']) for s in states)
    return {
        'm_Name': fields.get('m_Name', ''),
        'm_ControllerSize': CONTROLLER_BASE + STATE_SIZE * len(states) + CLIP_TREE_SIZE * trees,
        'm_Controller': baked,
        'm_TOS': sorted(names.items()),
        'm_AnimationClips': clips,
        'm_StateMachineBehaviourVectorDescription': {
            'm_StateMachineBehaviourRanges': [],
            'm_StateMachineBehaviourIndices': [],
        },
        'm_StateMachineBehaviours': [],
        'm_MultiThreadedStateMachine': True,
    }
