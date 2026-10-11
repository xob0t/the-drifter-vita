"""Bake an AudioMixerController (class 241) into the release form the player reads.

The YAML holds the mixer as the editor edits it: groups (243) with their effects (244, editor-only,
never written) and snapshots (245) that map parameter GUIDs to values. The player reads none of
that but m_MixerConstant, which the editor bakes at build time:

- Groups in depth-first order from the master group, children in m_Children order. Every group
  owns two parameters, volume (2i) and pitch (2i + 1), and points at its parent's index.
- Effects follow, group by group in that order, each group's m_Effects in order, chained by
  prevEffectIndex (the previous effect in the whole list, across groups). The master group's
  Attenuation is left out: the player applies the master volume on its own (the PC build of
  2020.3 leaves it out too, so the Attenuation in the YAML is AssetRipper's).
- Parameters are numbered as the effects are walked: an effect with wet mix enabled (every Send)
  gets one for its mix level, then one for each of its m_Parameters in YAML order. Sends point
  at their target's effect index; each effect a send targets gets a side chain buffer. (Here the
  targets are exactly the Receive and Duck Volume effects, so counting those would give the same
  3; an untargeted Duck Volume would tell the two apart.)
- Each snapshot is its name's CRC32 and one value per parameter.
- Names go into buffers of NUL-terminated strings with one more NUL at the end; exposed
  parameters are kept as CRC32 of their names, sorted by the parameter they expose (2018.2's
  order; the PC build of 2020.3 keeps m_ExposedParameters' order instead).
- GUIDs are the editor's (groups', effects', snapshots') as four words.

Effect types are the runtime's: negative for the mixer's own effects, the FMOD Ex DSP type for
FMOD's. The game's mixer has no snapshot transition overrides and no effect with both wet mix
and parameters; both raise rather than guess.
"""

import zlib

import convert

NONE = 0xFFFFFFFF
# The mixer's own effects.
BUILTIN_EFFECTS = {'Attenuation': -2, 'Send': -3, 'Receive': -4, 'Duck Volume': -5}
# FMOD Ex FMOD_DSP_TYPE by the editor's effect name. Normalize and SFX Reverb are checked
# against the reference; the rest follow FMOD Ex's enum.
FMOD_EFFECTS = {
    'Lowpass': 3, 'Highpass': 5, 'Echo': 6, 'Flange': 7, 'Distortion': 8, 'Normalize': 9, 'ParamEQ': 10,
    'Pitch Shifter': 11, 'Chorus': 12, 'Compressor': 16, 'SFX Reverb': 17, 'Lowpass Simple': 18,
    'Highpass Simple': 22,
}  # fmt: skip


def crc32(text):
    return zlib.crc32(text.encode('utf-8'))


def name_buffer(names):
    return list(b''.join(n.encode('utf-8') + b'\0' for n in names) + b'\0')


def flag(value):
    return convert.number(value, 'bool')


def fid(ref):
    return int(ref.get('fileID', 0))


def groups_of(master, docs):
    out, stack = [], [(fid(master), -1)]
    while stack:
        g, parent = stack.pop()
        out.append((docs[g], parent))
        stack.extend((fid(c), len(out) - 1) for c in reversed(docs[g].fields.get('m_Children') or []))
    return out


def constant(doc, asset):
    docs = asset.docs
    y = doc.fields
    groups = groups_of(y['m_MasterGroup'], docs)
    params = {}  # parameter GUID -> index

    def parameter(guid):
        if guid in params:
            raise ValueError(f'parameter {guid} is used twice')
        params[guid] = len(params)
        return params[guid]

    group_constants = []
    for g, parent in groups:
        f = g.fields
        group_constants.append(
            {
                'parentConstantIndex': parent,
                'volumeIndex': parameter(f['m_Volume']),
                'pitchIndex': parameter(f['m_Pitch']),
                'mute': flag(f['m_Mute']),
                'solo': flag(f['m_Solo']),
                'bypassEffects': flag(f['m_BypassEffects']),
            }
        )
    effects = []  # (effect doc, group index)
    for i, (g, parent) in enumerate(groups):
        for ref in g.fields.get('m_Effects') or []:
            e = docs[fid(ref)]
            if parent == -1 and e.fields['m_EffectName'] == 'Attenuation':
                continue
            effects.append((e, i))
    index_of = {e.file_id: i for i, (e, _) in enumerate(effects)}
    effect_constants, targets = [], set()
    for i, (e, group) in enumerate(effects):
        f = e.fields
        name = f['m_EffectName']
        kind = BUILTIN_EFFECTS.get(name, FMOD_EFFECTS.get(name))
        if kind is None:
            raise ValueError(f'effect {name!r}: type unknown (native plugins are not baked)')
        target = fid(f.get('m_SendTarget') or {})
        if target:
            targets.add(target)
        wet = flag(f.get('m_EnableWetMix', '0'))
        if wet and f.get('m_Parameters'):
            raise ValueError(f'effect {name!r}: wet mix and parameters, order unknown')
        effect_constants.append(
            {
                'type': kind,
                'groupConstantIndex': group,
                'sendTargetEffectIndex': index_of[target] if target else NONE,
                'wetMixLevelIndex': parameter(f['m_MixLevel']) if wet else NONE,
                'prevEffectIndex': i - 1 if i else NONE,
                'bypass': flag(f['m_Bypass']),
                'parameterIndices': [parameter(p['m_GUID']) for p in f.get('m_Parameters') or []],
            }
        )
    snapshots = [docs[fid(s)] for s in y['m_Snapshots']]
    snapshot_constants = []
    for s in snapshots:
        f = s.fields
        if f.get('m_TransitionOverrides'):
            raise ValueError(f'snapshot {f["m_Name"]}: transition overrides are not baked')
        values = [0.0] * len(params)
        for guid, i in params.items():
            if guid not in f['m_FloatValues']:
                raise ValueError(f'snapshot {f["m_Name"]}: no value for parameter {guid}')
            values[i] = convert.number(f['m_FloatValues'][guid], 'float')
        snapshot_constants.append(
            {'nameHash': crc32(f['m_Name']), 'values': values, 'transitionTypes': [], 'transitionIndices': []}
        )
    exposed = sorted(
        ((params[p['guid']], crc32(p['name'])) for p in y.get('m_ExposedParameters') or []), key=lambda x: x[0]
    )
    return {
        'groups': group_constants,
        'groupGUIDs': [convert.guid_words(g.fields['m_GroupID']) for g, _ in groups],
        'effects': effect_constants,
        'effectGUIDs': [convert.guid_words(e.fields['m_EffectID']) for e, _ in effects],
        'numSideChainBuffers': len(targets),
        'snapshots': snapshot_constants,
        'snapshotGUIDs': [convert.guid_words(s.fields['m_SnapshotID']) for s in snapshots],
        'groupNameBuffer': name_buffer(g.fields['m_Name'] for g, _ in groups),
        'snapshotNameBuffer': name_buffer(s.fields['m_Name'] for s in snapshots),
        'pluginEffectNameBuffer': name_buffer([]),
        'exposedParameterNames': [h for _, h in exposed],
        'exposedParameterIndices': [i for i, _ in exposed],
    }


def mixer(doc, ctx, asset):
    """The release AudioMixerController of `doc`, a document of `asset` (assets.Asset)."""
    out, _ = convert.convert(241, doc.fields, ctx)
    baked = constant(doc, asset)
    if set(baked) != set(out['m_MixerConstant']):
        raise ValueError(f'm_MixerConstant fields: {sorted(set(baked) ^ set(out["m_MixerConstant"]))}')
    out['m_MixerConstant'] = baked
    return out
