// 共享工具：查询归一化、分词与相对时间格式化。

export function normalizeQuery(raw) {
  return String(raw ?? '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

export function tokenize(text) {
  return text ? text.split(' ').filter(Boolean) : [];
}

export function formatRelativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '未知时间';
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}
