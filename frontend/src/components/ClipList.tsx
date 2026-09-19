import type { Clip } from '../api'
import { episodeCode, formatTimecode } from '../api'

type Props = {
  clips: Clip[]
  active: Clip | null
  keyword: string
  onSelect: (clip: Clip) => void
}

function Highlight({ text, keyword }: { text: string; keyword: string }) {
  const index = keyword ? text.toLowerCase().indexOf(keyword.toLowerCase()) : -1
  if (index < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded-sm bg-amber-200 px-0.5 text-inherit">
        {text.slice(index, index + keyword.length)}
      </mark>
      {text.slice(index + keyword.length)}
    </>
  )
}

function isSame(a: Clip, b: Clip | null): boolean {
  return (
    b !== null &&
    a.season === b.season &&
    a.episode === b.episode &&
    a.cue_index === b.cue_index
  )
}

export function ClipList({ clips, active, keyword, onSelect }: Props) {
  return (
    <ul className="flex flex-col gap-2">
      {clips.map((clip) => {
        const selected = isSame(clip, active)
        return (
          <li key={`${episodeCode(clip)}-${clip.cue_index}`}>
            <button
              type="button"
              onClick={() => onSelect(clip)}
              className={`w-full cursor-pointer rounded-lg border p-3 text-left transition ${
                selected
                  ? 'border-sky-400 bg-sky-50 ring-1 ring-sky-300'
                  : 'border-slate-200 bg-white hover:border-sky-300 hover:bg-slate-50'
              }`}
            >
              <div className="mb-1 flex items-center gap-2 font-mono text-xs text-slate-500">
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
                  {episodeCode(clip)}
                </span>
                <span>{formatTimecode(clip.start_ms)}</span>
              </div>
              <p className="text-sm leading-relaxed text-slate-900">
                <Highlight text={clip.zh} keyword={keyword} />
              </p>
              <p className="text-xs leading-relaxed text-slate-500">
                <Highlight text={clip.en} keyword={keyword} />
              </p>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
