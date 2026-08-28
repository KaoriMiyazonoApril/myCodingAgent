# 02: 打通受限 Workspace 选择纵向链路

**What to build:** 用户可以在 React 页面中浏览 Host 允许的目录根，逐层导航并选择一个真实
Linux/WSL workspace。Host 以稳定 JSON 和错误 envelope 返回目录信息，浏览器既不读取文件
内容，也不能越过启动时配置的 roots。

**Blocked by:** 01: 打通本地 Provider 配置纵向链路.

**Status:** ready-for-agent

- [ ] Host 接受一个或多个规范化 workspace roots，未提供时使用启动工作目录。
- [ ] Workspace command 每次只列一层目录，目录优先、按名称排序、包含隐藏目录，并在 500 项处返回 `truncated`。
- [ ] Picker 不跟随或返回 symlink，不返回普通文件，也不读取任何文件内容。
- [ ] containment 检查正确拒绝 `..`、绝对 sibling、共同字符串前缀和其他 root escape。
- [ ] 不存在、不可访问和 root 外路径使用不同稳定 error code，前端均显示可操作错误。
- [ ] React workspace picker 支持 roots、parent、entry navigation、当前选择和重新加载。
- [ ] WSL 路径原样显示为 Host POSIX path，不在浏览器中尝试 Windows/WSL 转换。
- [ ] Workspace picker 授权与 Runtime workspace validation 保持独立，UI 不把可浏览等同于可执行。
- [ ] 开发 CORS 只允许约定的 Vite origin；生产 same-origin 设计不引入宽泛 CORS。
- [ ] Host application tests 覆盖多个 root、排序、隐藏目录、截断、symlink、权限和 escape；React tests 覆盖导航与错误。

