// 渲染进程：搜索输入 -> 经 preload 暴露的 API 调主进程 -> 渲染结果列表。
import { createSearchController } from './search.js';
import { formatRelativeTime } from '../shared/utils.js';

const controller = createSearchController(window.notesApi);

const input = document.querySelector('#search-input');
const list = document.querySelector('#results');
const total = document.querySelector('#total');

input.addEventListener('input', async (event) => {
  const query = event.target.value.trim();
  if (!query) {
    list.replaceChildren();
    total.textContent = '';
    return;
  }
  const { total: count, items } = await controller.run(query, { limit: 20 });
  total.textContent = `${count} 条结果`;
  list.replaceChildren(
    ...items.map((item) => {
      const li = document.createElement('li');
      li.textContent = `${item.title} · ${formatRelativeTime(item.updatedAt)}`;
      return li;
    }),
  );
});
