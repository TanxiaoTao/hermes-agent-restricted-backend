# restricted 执行后端 —— 实现与设计复盘

> **项目定位**：本文档是面试作品「为 Hermes Agent 新增 `restricted` 执行后端」的技术复盘。
> 本项目为 **24 小时限时开发**，是面试作业中**两个项目之一**（另一个是建筑资料 RAG 问答助手）。
> 目标不是"生产级安全沙箱"，而是**按 Hermes 已有后端模式正确接入、能跑、有测试**。

---

## 一、restricted 是什么（一句话定位）

Hermes Agent 是一个会**自主调用 shell 执行命令**的 AI agent。它的执行后端（backend）决定命令跑在哪、有多安全。已有的后端：

| 后端 | 隔离级别 |
|---|---|
| `local` | **零隔离**——完全信任宿主机，继承全部环境变量、能访问整个文件系统 |
| `docker` / `singularity` | **重隔离**——容器/内核级，但重、要装 Docker、有镜像挂载复杂度 |
| **`restricted`（本次新增）** | **轻量应用层沙箱**——不引入容器，只做目录锁定 + 环境变量白名单 |

它解决的真实问题：**让 agent 干活，但别让它看到/碰到不该碰的东西**。
比如 agent 要分析一个陌生仓库、运行用户上传的代码——此时你不希望它读到宿主机环境变量里的
`*_API_KEY`、密码，也不希望它访问目录外的敏感文件。

核心 4 项安全能力：

1. **子进程执行** —— 每次命令 spawn 独立 `bash -c` 进程；
2. **超时强杀** —— 超过配置时间强制终止整个进程树；
3. **工作目录锁定** —— 命令只能在指定目录内执行，拦截 `..` / 符号链接穿越；
4. **环境变量白名单** —— 不透传宿主机完整 env，只放行白名单变量。

---

## 二、Hermes 的后端架构（为什么 restricted 长这样）

Hermes 所有后端都在 `tools/environments/` 目录，共享一个抽象基类：

```
tools/environments/
├── base.py          # BaseEnvironment(ABC) —— 统一执行流水线
├── local.py         # LocalEnvironment —— 本机执行
├── docker.py        # DockerEnvironment
├── ssh.py           # SSHEnvironment
├── restricted.py    # RestrictedEnvironment ← 本次新增
└── ...
```

**核心设计：模板方法模式。**

基类 `BaseEnvironment` 统一了"执行一条命令"的完整流程，子类**只实现两个钩子**：

```python
class BaseEnvironment(ABC):
    def execute(self, command, cwd="", *, timeout=None, ...) -> dict:
        # 统一流程：命令包装 → _run_bash 生成进程 → 轮询等待+超时 → 输出收集 → cwd 更新
        ...

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        """生成 bash 进程 —— 每个后端必须实现"""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self):
        """释放资源 —— 每个后端必须实现"""
```

所以 restricted 后端**不用重写超时轮询、不用重写输出收集、不用重写进程等待**——
这些全是基类的。它只需要回答"怎么 spawn 进程"（`_run_bash`）和"怎么清理"（`cleanup`）。

**这就是 Hermes 的"窄腰"设计**：核心执行流程在基类（一处），后端差异在子类（各管各的）。


---

## 三、代码实现逐部分讲解

### 3.1 路径校验（两个纯函数，restricted.py 顶部）

```python
def resolve_real_path(path: str) -> str:
    """expandvars/expanduser → abspath → realpath（跟随符号链接）"""
    expanded = os.path.expandvars(os.path.expanduser(path or ""))
    return os.path.realpath(os.path.abspath(expanded))

def is_path_within(root: str, target: str) -> bool:
    root_real = os.path.realpath(os.path.abspath(root))
    target_real = os.path.realpath(os.path.abspath(target))
    return os.path.commonpath([root_real, target_real]) == root_real
```

**为什么这样写**：

- **`realpath`（关键）**：`..` 会被 `abspath` 归一看穿，但**符号链接**不会被 `abspath` 看穿——
  攻击者在允许目录里建一个指向 `/etc` 的 symlink，`abspath` 看到的是 `允许目录/link`，以为安全，
  实际进程 `cd` 进去就出了边界。所以必须 `realpath`（解析符号链接后的真实路径）。
- **`commonpath` 而不是 `startswith`（关键）**：`startswith` 会被前缀碰撞骗过——
  `/tmp/root` 是 `/tmp/root2` 的前缀，`startswith` 会误判"root2 在 root 内"。
  `commonpath([root, target]) == root` 才是正确的"包含"判定。
- **跨盘符/异常 fail-closed**：`commonpath` 对不同盘符（Windows `C:` vs `D:`）抛 `ValueError`，
  被捕获后返回 `False`（拒绝），宁可误拒不可放行。

### 3.2 构造 `__init__`

```python
def __init__(self, cwd="", timeout=60, env=None, allowed_workdir=None, env_allowlist=None):
    if not allowed_workdir:
        raise ValueError("RestrictedEnvironment requires an allowed_workdir")
    root = resolve_real_path(str(allowed_workdir))
    if not os.path.isdir(root):
        raise ValueError("allowed_workdir is not a directory")
    self._allowed_root = root
    self._env_allowlist = tuple(...)
    target = resolve_real_path(cwd) if cwd else root     # 初始 cwd 越界直接拒绝
    if not is_path_within(root, target):
        raise ValueError("cwd escapes allowed workdir")
    super().__init__(cwd=target, timeout=timeout, env=env)
    self._prefer_nonlogin = True   # 跳过登录 shell 快照
```

**为什么跳过 `init_session`（登录 shell 快照）**：

`LocalEnvironment` 的 `init_session` 会 source 用户的 `~/.bashrc` / `~/.profile`，把登录 shell 的
环境变量、PATH 快照下来，每次命令前重新 source。**这对 restricted 是致命的**——它会绕过白名单，
把宿主机环境重新带进子进程。所以 restricted **故意跳过快照**，用 `_prefer_nonlogin=True` 强制走
非登录 `bash -c` 路径。

### 3.3 子进程执行 `_run_bash`

```python
def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
    bash = _find_bash()                       # 复用 local 的 bash 解析
    cwd = self._ensure_cwd_valid()            # spawn 前再校验一次 cwd
    proc = subprocess.Popen(
        [bash, "-c", cmd_string],
        text=True, env=self._build_whitelisted_env(),
        encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,   # 输出合并
        stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
        start_new_session=True, cwd=cwd,                    # 独立进程组 + 锁定 cwd
        **({"creationflags": windows_hide_flags()} if _IS_WINDOWS else {}))
    ...
```

**关键点**：

- `start_new_session=True` —— **让子进程成为独立进程组组长**。这是超时强杀的前提：只有独立进程组，
  才能 `killpg` 整组杀掉（连孙进程一起），否则只能杀 bash 本身、孙进程变孤儿继续跑。
- `stderr=STDOUT` —— 输出合并，简化收集（和 local 一致）。

### 3.4 超时强杀 `_kill_process` / `_force_kill_process`

```python
def _kill_process(self, proc):
    (_kill_process_windows if _IS_WINDOWS else _kill_process_group_posix)(proc)

def _force_kill_process(self, proc):
    if _IS_WINDOWS: return self._kill_process(proc)
    os.killpg(pgid, signal.SIGKILL)   # 无 TERM 宽限，直接 SIGKILL
```

**为什么复用 local 的实现而不是自己写**：进程组 kill 是 POSIX 的细节地狱——
TERM → 等 1s → KILL → 扫 `setsid` 逃逸的孙进程、处理 "child 共享了父进程组" 的边界、Windows 上要
身份校验防 PID 复用……`local.py` 已经把这些坑全踩过了。restricted 直接复用，不重复造轮子
（Hermes 的 "extend, don't duplicate" 原则）。

超时本身由基类 `execute` → `_wait_for_process` 处理：轮询进程直到 deadline，超时调 `_kill_process`，
返回 `returncode 124` + "[Command timed out after Ns]"。



### 3.5 环境白名单 `_build_whitelisted_env`

```python
def _build_whitelisted_env(self) -> dict:
    allowed = {name.upper() for name in (*self._env_allowlist, *_ALWAYS_ALLOW)}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}   # 只取白名单交集

    path_key = _path_env_key(env)
    if path_key is None:
        env["PATH"] = _SANE_PATH            # PATH 兜底，保证命令能跑
    else:
        env[path_key] = _prepend_git_bash_dirs(_append_missing_sane_path_entries(env[path_key]))

    scrubbed = {}
    _filter_secret_env(env, scrubbed, unwrap_force=False,
                       plugin_strip=_plugin_terminal_env_strip_keys())   # 纵深防御
    return _finalize_child_env(scrubbed)
```

**为什么"白名单 ∩ os.environ" + 再叠加 secret 清洗**：

- 核心：**永不全量透传 `os.environ`**，只取「白名单 ∪ `_ALWAYS_ALLOW`（PATH/HOME/LANG/LC_ALL/TZ）」的交集；
- `_ALWAYS_ALLOW` 保证即使白名单为空，子进程也有可用的 PATH/HOME 不至于一上来就 127；
- 最后再叠 `_filter_secret_env`（Hermes 的 provider 秘钥剥离）是**纵深防御**：万一用户把
  `OPENAI_API_KEY` 写进了白名单，这一步仍会把它剥掉（符合 Hermes "secret 永不外泄" 的不变量）。

### 3.6 入口 `execute`（前置校验）

```python
def execute(self, command, cwd="", *, timeout=None, ...) -> dict:
    try:
        locked = self._validate_cwd(cwd or self.cwd)
    except ValueError as exc:
        return {"output": f"[RestrictedEnvironment refused: {exc}]", "returncode": 126}
    self.cwd = locked
    return super().execute(command, cwd=locked, timeout=timeout, ...)
```

越界时**返回结构化结果（returncode 126）而不是抛未捕获异常**——这样上层 terminal 工具能把它当
"命令失败"展示，而不是让整个 agent 崩溃。

### 3.7 MSYS cwd 归一化 `_extract_cwd_from_output`（真实 E2E 抓出的 bug）

```python
def _extract_cwd_from_output(self, result):
    prev_cwd = self.cwd
    super()._extract_cwd_from_output(result)      # 基类从 stdout marker 解析 cwd
    if self.cwd != prev_cwd:
        normalized = _msys_to_windows_path(self.cwd)   # /c/Users/... → C:\Users\...
        if normalized and os.path.isdir(normalized) and is_path_within(self._allowed_root, normalized):
            self.cwd = normalized
        else:
            self.cwd = prev_cwd                   # 越界则回滚
```

**这是真实子进程才暴露的 bug**：Git Bash 的 `pwd` 输出的是 **MSYS 路径**（`/c/Users/...`），基类会把
它写回 `self.cwd`，下一次 `_validate_cwd` 就误判"越界"、导致连续命令全部被拒。`LocalEnvironment`
有同样的归一化逻辑（`_msys_to_windows_path`），restricted 必须照做——**这正是"复用它、别自己漏掉"的教训**。

---

## 四、注册链路（为什么有这么多处要改）

restricted 要"像 local/docker/ssh 一样被选用"，需要改 **1 个工厂 + 5 处名单 + 3 处配置桥接**：

| 位置 | 作用 |
|---|---|
| `tools/terminal_tool_backends.py::_ENV_BUILDERS` | **真正的工厂分发**：`"restricted": _build_restricted_env` |
| `_BACKEND_SPECS` | 需求检查（workdir 未配置时 fail-loud） |
| `agent/terminal_env_registry.py::BUILTIN_BACKEND_NAMES` | 插件禁止占用内置名 |
| `tools/terminal_tool_config.py::_BUILTIN_BACKENDS` | 分类（**不进** `_CONTAINER_BACKENDS`，它是本机后端） |
| `hermes_cli/doctor_tools.py` / `setup_terminal.py` | `hermes doctor` 识别 + setup 向导 |
| `hermes_cli/cli_config_load.py` + `gateway/run.py` + `hermes_cli/config.py` | **三处 config→env 桥接** |
| `tools/terminal_tool.py::_get_env_config` | 读取 `TERMINAL_RESTRICTED_WORKDIR` / `_ALLOWLIST` |

**为什么三处桥接必须同步**：Hermes 的配置流向是
`config.yaml` →（三处桥接）→ `TERMINAL_*` 环境变量 → `_get_env_config()` 读取。
CLI、gateway、dashboard 三个入口各自 bridge 一次，漏一处就会"CLI 能用、gateway 不能用"。
仓库里专门有个测试 `test_terminal_config_env_sync.py` 强制三处 map 键一致——这是 Hermes 的
"fail loud at integration boundaries" 原则的体现。

---

## 五、为什么这样设计（跟 Hermes 设计哲学的关系）

| 设计决策 | 对应的 Hermes 原则 |
|---|---|
| 继承 `BaseEnvironment`、只实现 `_run_bash`/`cleanup` | **模板方法模式 + 窄腰**：核心流程一处，后端差异各管各 |
| 复用 `local` 的 `_find_bash`/进程组 kill/secret 清洗 | **extend, don't duplicate**：不重复造轮子 |
| 作为内置后端（非插件）接入 `_ENV_BUILDERS` | **Footprint Ladder**：扩展现有代码，零新增核心面 |
| 越界返回 126 结构化结果，不抛未捕获异常 | 工具层"命令失败"语义，而非 agent 崩溃 |
| 白名单后仍叠加 `_filter_secret_env` | **secret 永不外泄**不变量 |
| 三处 config→env 桥接同步 | **fail loud at integration boundaries**（有测试强制） |

---

## 六、验证结果

| 验证 | 结果 |
|---|---|
| 单元测试 `test_restricted_environment.py` | **19 passed, 1 skipped**（skip=Windows 软链接权限） |
| 真实 bash 子进程 E2E | **5 项全 PASS**（子进程 / 目录锁定 / 超时 rc=124 / 穿越拒绝 126 / 白名单） |
| 通过 Hermes 真实 `terminal_tool` 链路 | `echo` 成功 + 越界拒绝 |

过程中靠"搭真实 bash 环境跑 E2E"，**抓出并修复了 2 个单测覆盖不到的真实 bug**（MSYS cwd 归一化、
restricted 默认 cwd 被设成 `/root`）——这正是"真实运行时验证"的价值。

---

## 七、边界与局限（必须声明）

- **不是强隔离沙箱**：无 seccomp / namespace / cgroup，子进程仍与宿主共享内核与 UID；
- 目录锁定 + 白名单只能**缓解**，不能**杜绝**恶意命令用绝对路径读写 cwd 之外的文件；
- 存在 TOCTOU 竞态（校验后、spawn 前被替换成 symlink 的窗口），进程级方案无法彻底消除。
