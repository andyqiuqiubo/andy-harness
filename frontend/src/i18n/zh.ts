export default {
  // Navigation
  'nav.chat': '开始对话',
  'nav.settings': '设置',
  'nav.hello': 'Hello',

  // Settings
  'settings.title': '设置',
  'settings.back': '← 返回对话',
  'settings.tab.providers': '模型 Provider',
  'settings.tab.session': '会话设置',
  'settings.tab.plugins': '插件管理',
  'settings.tab.general': '通用设置',

  // Providers
  'providers.title': '模型 Provider',
  'providers.add': '+ 新增',
  'providers.cancel': '取消',
  'providers.name': '名称',
  'providers.baseUrl': 'Base URL',
  'providers.apiKey': 'API Key',
  'providers.models': '模型列表 (逗号分隔)',
  'providers.submit': '添加',
  'providers.test': '测试连接',
  'providers.testing': '测试中...',
  'providers.testOk': '✓ 连通',
  'providers.testFail': '✗ 失败',
  'providers.delete': '删除',
  'providers.edit': '编辑',
  'providers.enable': '启用',
  'providers.disable': '禁用',
  'providers.empty': '暂无 Provider，点击「+ 新增」添加自定义模型',
  'providers.enabled': '已启用',
  'providers.disabled': '已禁用',
  'providers.hasApiKey': '🔑 已配置',
  'providers.noApiKey': '⚠ 未配置密钥',

  // Session settings
  'session.title': '会话级设置',
  'session.model': '默认模型',
  'session.temperature': '温度 (Temperature)',
  'session.systemPrompt': '系统提示词',
  'session.save': '保存设置',
  'session.saved': '设置已保存',

  // Plugins
  'plugins.title': '插件管理',
  'plugins.core': '核心',
  'plugins.empty': '暂无已加载的插件',
  'plugins.config': '插件配置',
  'plugins.saveConfig': '保存配置',

  // General
  'general.title': '通用设置',
  'general.theme': '主题',
  'general.themeLight': '☀️ 亮色',
  'general.themeDark': '🌙 暗色',
  'general.language': '语言',

  // Chat
  'chat.placeholder': '输入消息... (Enter 发送, Shift+Enter 换行)',
  'chat.send': '发送',
  'chat.stop': '停止生成',
  'chat.connected': '已连接',
  'chat.connecting': '连接中...',
  'chat.disconnected': '已断开',
  'chat.reconnecting': '重连中...',

  // Sessions
  'sessions.title': '会话',
  'sessions.new': '+ 新建',
  'sessions.rename': '重命名',
  'sessions.archive': '归档',
  'sessions.empty': '暂无会话，点击「新建」创建',
  'sessions.showArchived': '显示归档',
  'sessions.hideArchived': '隐藏归档',
  'sessions.archived': '已归档',
  'sessions.delete': '删除',
  'sessions.deleteAll': '清空全部会话',
  'sessions.confirmDelete': '确定删除此会话？此操作不可撤销。',
  'sessions.confirmDeleteAll': '确定删除所有会话？此操作不可撤销。',
} as Record<string, string>
