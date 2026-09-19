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
      {/* 深色底上用品牌红高亮，比浅黄更贴合整体配色 */}
      <mark className="rounded-sm bg-brand px-0.5 text-white">
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

/**
 * 结果卡片网格（Netflix 风格，AGENTS.md 第 2 节「UI 风格」）。
 *
 * 每张卡片 = 该集封面（抽帧得到）+ 双语台词。封面承担了主要观感，
 * 因此悬停时轻微放大、选中时套一圈品牌红。
 *
 * 手机上保持两列：一列的话一屏只能看到两三条，搜索结果的浏览效率反而下降。
 */
export function ClipList({ clips, active, keyword, onSelect }: Props) {
  return (
    <ul className="grid grid-cols-2 gap-3">
      {clips.map((clip) => {
        const selected = isSame(clip, active)
        return (
          <li key={`${episodeCode(clip)}-${clip.cue_index}`}>
            <button
              type="button"
              onClick={() => onSelect(clip)}
              aria-current={selected ? 'true' : undefined}
              className={`group block w-full cursor-pointer overflow-hidden rounded-md text-left transition ${
                selected
                  ? 'bg-panel ring-2 ring-brand'
                  : 'bg-panel/70 ring-1 ring-line hover:bg-panel-hover hover:ring-white/25'
              }`}
            >
              <div className="relative aspect-video w-full overflow-hidden bg-black">
                <img
                  src={clip.cover}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  /* 片头段（演职员表叠加）故意不生成缩略图，个别抽帧也可能失败：
                     第一次失败退回该集封面，第二次才隐藏 —— 不留破图图标 */
                  onError={(event) => {
                    const img = event.currentTarget
                    if (img.dataset.fallback !== '1') {
                      img.dataset.fallback = '1'
                      img.src = clip.poster
                    } else {
                      img.style.visibility = 'hidden'
                    }
                  }}
                  className="h-full w-full object-cover transition duration-300 group-hover:scale-[1.06]"
                />

                <span className="absolute top-1.5 left-1.5 rounded bg-black/75 px-1.5 py-0.5 font-mono text-[10px] font-medium text-white/90">
                  {episodeCode(clip)}
                </span>
                <span className="absolute top-1.5 right-1.5 rounded bg-black/75 px-1.5 py-0.5 font-mono text-[10px] text-white/80">
                  {formatTimecode(clip.start_ms)}
                </span>

                {selected && (
                  <span className="absolute bottom-1.5 left-1.5 flex items-center gap-1 rounded bg-brand px-1.5 py-0.5 text-[10px] font-semibold text-white">
                    ▶ 播放中
                  </span>
                )}
              </div>

              <div className="p-2.5">
                <p className="line-clamp-2 text-[13px] leading-snug text-white">
                  <Highlight text={clip.zh} keyword={keyword} />
                </p>
                <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted italic">
                  <Highlight text={clip.en} keyword={keyword} />
                </p>
              </div>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
