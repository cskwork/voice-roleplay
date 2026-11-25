import { useEffect, useRef, useState, type CSSProperties } from 'react'
import faceUrl from './ham/ham-face.svg'
import type { CallLine } from './callSession'

/** The words of a call, as bubbles. With `gloss` every English word can be tapped for its meaning. */
export function TalkList({ lines, canHear, hear, gloss }: { lines: CallLine[]; canHear?(line: CallLine): boolean; hear?(line: CallLine): void; gloss?(word: string, responseId: string): Promise<string | null> }) {
  const [tip, setTip] = useState<{ text: string; at: DOMRect; key: string } | null>(null)
  const asked = useRef('')
  useEffect(() => {
    if (!tip) return
    const close = () => setTip(null)
    const timer = setTimeout(close, 4000)
    window.addEventListener('scroll', close, { capture: true, passive: true })
    return () => {
      clearTimeout(timer)
      window.removeEventListener('scroll', close, { capture: true })
    }
  }, [tip])

  const ask = async (el: HTMLElement, word: string, responseId: string) => {
    if (!gloss) return
    const key = (asked.current = `${responseId}:${word}:${performance.now()}`)
    const at = el.getBoundingClientRect()
    setTip({ text: '뜻을 찾고 있어요…', at, key })
    const ko = await gloss(word, responseId).catch(() => null)
    if (asked.current === key) setTip({ text: ko ?? '뜻을 가져오지 못했어요', at, key })
  }

  return (
    <>
      <ol className="hc-talk">
        {lines.map((l) => (
          <li key={l.key} className={l.who === 'me' ? 'me' : ''}>
            {l.who === 'ham' && <img className="hc-face" src={faceUrl} alt="" width={44} height={44} />}
            <div className="bub">
              <span className="sr-only">{l.who === 'ham' ? '햄찌' : '나'}</span>
              {hear && canHear?.(l) && (
                <button type="button" className="say" aria-label={`${l.who === 'ham' ? '다시 듣기' : '내 목소리 듣기'}: ${l.who === 'ham' ? l.en : l.text}`} onClick={() => hear(l)}>
                  <SpeakerIcon />
                </button>
              )}
              {l.who === 'ham' && l.ko && <span className="ko">{l.ko}</span>}
              <span className="en" lang="en">
                {l.who === 'me'
                  ? l.text
                  : !gloss
                  ? l.en
                  : l.en.split(' ').map((w, i) => {
                      const key = w.toLowerCase().replace(/’/g, "'").match(/[a-z']+/)?.[0]
                      return (
                        <span key={i}>
                          {i > 0 && ' '}
                          {key ? (
                            <button type="button" className="word" onClick={(e) => void ask(e.currentTarget, key, l.responseId)}>
                              {w}
                            </button>
                          ) : (
                            w
                          )}
                        </span>
                      )
                    })}
              </span>
            </div>
          </li>
        ))}
      </ol>
      {tip && (
        <div className="hc-wordtip" role="status" style={{ '--x': `${tip.at.left + tip.at.width / 2}px`, '--y': `${tip.at.bottom + 6}px` } as CSSProperties}>
          {tip.text}
        </div>
      )}
    </>
  )
}

function SpeakerIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 9.5h3.5L12 5v14l-5.5-4.5H3Z" />
      <path d="M15 9a4 4 0 0 1 0 6M17.5 6.5a7.5 7.5 0 0 1 0 11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}
