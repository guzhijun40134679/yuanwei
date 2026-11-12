"""产品自己的错误类（研究里没有对应的）。出错不冒充空结果：每一种都能被调用方认出来。"""
from .storeerr import StoreError


class YuanweiError(Exception):
    code = "error"


class MissingVector(YuanweiError):
    """混合模式缺向量：不悄悄退成关键词。"""
    code = "missing_vector"


class VectorIdentityMismatch(YuanweiError):
    code = "vector_identity_mismatch"


class BadCursor(YuanweiError):
    """续读游标不存在、过期、和当前请求对不上。"""
    code = "bad_cursor"


class NotVisible(YuanweiError):
    """消息存在，但不在这次读取的截点里（旧格式截点：所属的步在截点之后才结束；新格式截点：比截点的原话上界更晚入库）。"""
    code = "not_visible"


class BadCut(YuanweiError):
    """调用方自己传的截点不合法：不存在、不是这个工作区的、超前、两个上界组合不一致、格式不认识。一律拒绝，不悄悄换成当前状态。"""
    code = "bad_cut"


class HistoryDeleted(BadCut):
    """这个工作区在截点（或游标）之后发生过删除：旧截点、旧游标、旧返回都不再有效，要重新查询。不静默换成新截点。"""
    code = "history_deleted"


class DeletionIncomplete(YuanweiError):
    """有没做完的删除（清理中）：这个工作区暂时不开放读取。"""
    code = "deletion_incomplete"


class ExcludedThisTurn(YuanweiError):
    """这条消息属于当前这一轮（可信接头绑定的），不作为历史交出。"""
    code = "excluded_this_turn"


class CursorExcluded(BadCursor):
    """续读游标里还没交的材料，有现在这一轮要排除的消息：旧游标不能绕过新的排除规则，要求重新查询（不悄悄重算剩下的页）。"""
    code = "cursor_excluded"


class NotFound(YuanweiError):
    code = "not_found"


class Refused(YuanweiError):
    """这一轮明确不许读历史（拒用记忆）。"""
    code = "refused"


class Paused(YuanweiError):
    code = "paused"


class ConfigError(YuanweiError):
    code = "config"


class ProviderError(YuanweiError):
    code = "provider"


class WriteBusy(YuanweiError):
    """数据库写忙，原话已写进待补文件。"""
    code = "write_busy"


class BadQuery(YuanweiError, ValueError):
    """recall 的可选关键词不合规（类型、条数、长度）：在任何检索和向量调用之前报错，不悄悄截断。"""
    code = "bad_query"
