import type { Clip } from '../api'
import { episodeCode, formatTimecode, fragmentSrc } from '../api'

/**
 * 片段播放器。
 *
 * `key` 用 Media Fragment URI：切片段时重建 <video>，浏览器据此重新定位。
 * 不加自写暂停逻辑——end 边界由浏览器原生遵守（AGENTS.md 第 4 节，已实测）。
 */
export function ClipPlayer({ clip }: { clip: Clip | null }) {
  if (clip === null) {
    return (
      <div className="flex aspect-video w-full items-center justify-center rounded-xl border border-dashed border-slate-300 bg-slate-50 text-sm text-slate-400">
        在左侧选择一条台词，这里会播放对应片段
      </div>
    )
  }

  const src = fragmentSrc(clip)

  return (
    <div className="flex flex-col gap-3">
      <video
        key={src}
        src={src}
        controls
        autoPlay
        className="aspect-video w-full rounded-xl bg-black"
      />
      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <div className="mb-2 flex items-center gap-2 font-mono text-xs text-slate-500">
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
            {episodeCode(clip)}
          </span>
          <span>
            {formatTimecode(clip.start_ms)} – {formatTimecode(clip.end_ms)}
          </span>
          <span className="text-slate-300">
            #{clip.cue_index} · {(clip.end_ms - clip.start_ms) / 1000}s
          </span>
        </div>
        <p className="text-base leading-relaxed text-slate-900">{clip.zh}</p>
        <p className="mt-1 text-sm leading-relaxed text-slate-500 italic">
          {clip.en}
        </p>
      </div>
    </div>
  )
}
