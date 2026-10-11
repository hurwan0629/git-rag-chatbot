export function unicodeSlice(text, start, end) { return Array.from(text).slice(start, end).join(''); }
export function safeGithubUrl(value) {
  try { const u = new URL(value); return u.protocol === 'https:' && u.hostname === 'github.com' && !u.username && !u.password && !u.port ? u.href : null; }
  catch { return null; }
}
export function progressLabel(progress) {
  if (!progress.unit || progress.total === null) return '총량 확인 중';
  return `${progress.current} / ${progress.total} ${progress.unit}${progress.percent === null ? '' : ` · ${progress.percent.toFixed(1)}%`}`;
}
