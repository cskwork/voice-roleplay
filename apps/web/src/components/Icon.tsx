const PATHS = {
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-7 9a7 7 0 0 0 14 0M12 19v3',
  micOff: 'M3 3l18 18M9 9v3a3 3 0 0 0 5.1 2.1M15 9.3V6a3 3 0 0 0-5.7-1.3M19 12a7 7 0 0 1-1.2 3.9M5 12a7 7 0 0 0 11 5.7M12 19v3',
  speaker: 'M4 9v6h4l5 4V5L8 9H4Zm12.5-.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  circle: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z',
  stop: 'M7 7h10v10H7z',
  pause: 'M8 5v14M16 5v14',
  play: 'M7 5l12 7-12 7V5Z',
  hint: 'M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V16h5v-.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3Z',
  replay: 'M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4',
  slow: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v4l3 2',
  close: 'M6 6l12 12M18 6L6 18',
  alert: 'M12 9v4m0 4h.01M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  record: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z',
  send: 'M4 12l16-8-6 16-2.5-6.5L4 12Z',
  edit: 'M4 20h4L19 9l-4-4L4 16v4Z',
  back: 'M15 5l-7 7 7 7',
  hand: 'M8 13V5.5a1.5 1.5 0 0 1 3 0V11m0-6.5a1.5 1.5 0 0 1 3 0V11m0-4.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-.5a6 6 0 0 1-4.9-2.6L3 15.5a1.5 1.5 0 0 1 2.4-1.8L8 16',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-11v6m0-9h.01',
  arrowRight: 'M5 12h14M13 6l6 6-6 6',
  arrowDown: 'M12 5v14M6 13l6 6 6-6',
  chevronDown: 'M6 9l6 6 6-6',
  headset: 'M4 15v-3a8 8 0 0 1 16 0v3M4 15a2 2 0 0 0 2 2h1v-5H6a2 2 0 0 0-2 2v1Zm16 0a2 2 0 0 1-2 2h-1v-5h1a2 2 0 0 1 2 2v1Z',
} as const

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 20, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
