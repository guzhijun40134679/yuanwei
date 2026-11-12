"""钩子先认工作区，再碰插件状态（补充施工单·乙 §4）。真机看到过：测试文件夹的项目级钩子在同一时段另外 3 个 Codex 会话里也触发了。
所以在登记、记日志正文、解析控制、读对话记录、读写记忆之前，先问「这个事件是不是发生在装接头时绑定的工作区里」。

* 装接头时绑定一个确定的工作区根目录；事件给的 cwd 规范化以后和它比。**路径按目录层级比，不按字符串前缀**（`/a/b` 和 `/a/bc` 不是一回事）。
* 符号链接：两边都先解析成真实路径再比。
* 兄弟目录：不属于。git 工作树（别处的另一份检出）：不属于，除非装接头时把它明确列进 extra_roots。
* 事件没有 cwd、不是绝对路径、不是文字：不属于（不猜）。
* 不能用来代替的：钩子脚本所在的目录、进程碰巧的 cwd、模型参数、简单的字符串前缀。
外部的、或归属说不清的事件，接头对宿主返回中性结果、不给插件状态留任何痕迹。
"""
import os


def _real(path):
    return os.path.realpath(path)


class WorkspaceFilter:
    def __init__(self, root, extra_roots=()):
        if not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError("工作区根目录必须是绝对路径")
        self.roots = tuple(_real(r) for r in (root,) + tuple(extra_roots))

    def inside(self, cwd):
        """返回 (属不属于, 原因)。原因是固定的几个词，不带路径本身。"""
        if cwd is None:
            return False, "cwd_missing"
        if not isinstance(cwd, str) or not cwd:
            return False, "cwd_invalid"
        if not os.path.isabs(cwd):
            return False, "cwd_not_absolute"
        c = _real(cwd)
        for r in self.roots:
            if c == r or c.startswith(r.rstrip(os.sep) + os.sep):
                return True, "inside"
        return False, "outside"
