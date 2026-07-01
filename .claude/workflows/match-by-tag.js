export const meta = {
  name: 'match-by-tag',
  description: 'Tiered scene matching for [Monitored] performers: Haiku triage shrinks each worksheet, Opus judges the shortlist by cover. Returns confirmed scene->clip links for the caller to write+enrich.',
  phases: [
    { title: 'Triage', detail: 'Haiku: scrape worksheet, drop junk, shortlist', model: 'haiku' },
    { title: 'Match', detail: 'Opus: cover-validate the shortlist, confirm', model: 'opus' },
  ],
}

// args: array of performer ids (resolve via `clipstores-scraper performers --tag` in
// the caller and pass them in). Each id runs triage -> match with no barrier.
// Tolerate args arriving as a real array, a JSON string, or a comma/space list.
let _raw = args
if (typeof _raw === 'string') {
  try {
    _raw = JSON.parse(_raw)
  } catch {
    _raw = _raw.split(/[,\s]+/)
  }
}
const IDS = (Array.isArray(_raw) ? _raw : _raw ? [_raw] : [])
  .map((x) => String(x).trim())
  .filter(Boolean)
if (!IDS.length) {
  log('No performer ids in args. Pass e.g. {name:"match-by-tag", args:["137","182"]}.')
  return { results: [], failed: [] }
}

const TRIAGE_SCHEMA = {
  type: 'object',
  required: ['performer_id', 'clean', 'gray', 'dropped_count'],
  additionalProperties: false,
  properties: {
    performer_id: { type: 'string' },
    clean: { type: 'array', items: shortItem() },
    gray: { type: 'array', items: shortItem() },
    dropped_count: { type: 'integer' },
  },
}
function shortItem() {
  return {
    type: 'object',
    required: ['scene_id', 'clip_url'],
    additionalProperties: false,
    properties: {
      scene_id: { type: 'string' },
      clip_url: { type: 'string' },
      clip_title: { type: 'string' },
      why: { type: 'string' },
    },
  }
}

const MATCH_SCHEMA = {
  type: 'object',
  required: ['performer_id', 'confirmed', 'no_match_count', 'summary'],
  additionalProperties: false,
  properties: {
    performer_id: { type: 'string' },
    confirmed: {
      type: 'array',
      items: {
        type: 'object',
        required: ['scene_id', 'clip_url', 'verified'],
        additionalProperties: false,
        properties: {
          scene_id: { type: 'string' },
          clip_url: { type: 'string' },
          clip_title: { type: 'string' },
          verified: { type: 'string' },
        },
      },
    },
    uncertain: {
      type: 'array',
      items: {
        type: 'object',
        required: ['scene_id'],
        additionalProperties: false,
        properties: {
          scene_id: { type: 'string' },
          clip_url: { type: 'string' },
          note: { type: 'string' },
        },
      },
    },
    no_match_count: { type: 'integer' },
    summary: { type: 'string' },
  },
}

const triagePrompt = (pid) =>
  `You are the cheap Haiku triage stage before an Opus matcher, running in ` +
  `AGGRESSIVE mode. Performer ${pid}. Write NOTHING; shrink a big worksheet to a ` +
  `shortlist, leaning toward forwarding (the Opus matcher makes the final call).\n` +
  `1. Scrape: \`uv run clipstores-scraper candidates "${pid}" > /tmp/cand_${pid}.json 2>/dev/null\` then Read that file. ` +
  `Each scene has a numeric "scene_id", plus scene_basename, scene_duration, scene_date, candidates[] (clip_title, clip_url, source, title_score, duration_delta, date_delta, gate). ` +
  `In your output the scene_id field MUST be that numeric "scene_id" — NEVER the scene_basename/filename.\n` +
  `2. Keep at most the SINGLE best candidate per scene and bucket it:\n` +
  `   - clean: exact/near-exact title (after censorship/abbrev normalization) AND duration_delta small (<=120s or 0).\n` +
  `   - gray: ANY plausible lead — an obvious rename/abbreviation, OR duration_delta <= ~120s even with a weak title, OR a clear theme/keyword overlap. When unsure, forward it as gray.\n` +
  `   - drop: only genuine noise.\n` +
  `3. Still drop these even in aggressive mode: any clip_url that is the best candidate for 3+ different scenes (duration-collision trap); opaque hash/date basenames with only coincidental duration overlap and no title signal.\n` +
  `Return ONLY JSON: performer_id="${pid}", clean[] and gray[] of {scene_id, clip_url, clip_title, why}, and dropped_count.`

const matchPrompt = (pid, shortlist) =>
  `You are the Opus matcher in AGGRESSIVE mode. Performer ${pid}. Triage already ` +
  `shrank the worksheet to this shortlist (do NOT re-scrape):\n\n` +
  JSON.stringify(shortlist) +
  `\n\nLean toward confirming. Confirm a candidate when ANY one holds and nothing ` +
  `contradicts it: (a) exact/near-exact title after normalizing censorship swaps + ` +
  `abbreviations (Forced->Encouraged, Poppers->Aroma/Breath, Pee->Whiz, ` +
  `Blackmail->Black*mail, Mesmerized->****d, leading-letter crops, word reorders, ` +
  `"part N"); (b) duration_delta <= ~120s AND a plausible title/theme overlap; ` +
  `(c) a cover that shows the same performer in the same scene moment.\n` +
  `- "clean" items: confirm on title+duration; no cover needed.\n` +
  `- "gray" items: run \`uv run clipstores-scraper images <scene_id> <clip_url>\` and ` +
  `Read BOTH covers. Use the cover to REJECT contradictions (different ` +
  `performer/set/outfit/era), but do NOT require a cover — if the cover is ` +
  `unavailable yet title+duration is strong, keep it.\n` +
  `The one hard rule aggression must not break: reject the duration-collision trap ` +
  `(one clip claimed by many scenes on duration alone). One clip backs at most one ` +
  `scene. Return the structured match object: performer_id="${pid}", confirmed[] ` +
  `(scene_id [the numeric id from the shortlist, never a filename], clip_url, ` +
  `clip_title, verified one of "exact-title"/"cover"/"title+duration"), uncertain[], ` +
  `no_match_count, summary.`

const results = await pipeline(
  IDS,
  (pid) =>
    agent(triagePrompt(pid), {
      label: `triage:${pid}`,
      phase: 'Triage',
      model: 'haiku',
      schema: TRIAGE_SCHEMA,
    }),
  (shortlist, pid) => {
    const n = (shortlist?.clean?.length || 0) + (shortlist?.gray?.length || 0)
    if (!n) {
      // Empty shortlist -> skip Opus entirely (the whole point of triage).
      return {
        performer_id: pid,
        confirmed: [],
        uncertain: [],
        no_match_count: shortlist?.dropped_count || 0,
        summary: 'triage forwarded nothing — no Opus spend',
      }
    }
    return agent(matchPrompt(pid, shortlist), {
      label: `match:${pid}`,
      phase: 'Match',
      model: 'opus',
      schema: MATCH_SCHEMA,
    })
  }
)

const ok = results.filter(Boolean)
const failed = IDS.filter((pid, i) => !results[i])
const total = ok.reduce((n, r) => n + (r.confirmed ? r.confirmed.length : 0), 0)
log(`Done: ${ok.length}/${IDS.length} performers, ${total} confirmed. Failed: ${failed.join(', ') || 'none'}`)
return { results: ok, failed }
