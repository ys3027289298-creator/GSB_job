"""题目台账自动化：建题、记录 A/B、重置环境、生成提交字段。

    python tools/task.py new T001 --workspace <dir> --prompt-file <file> [选项]
    python tools/task.py record T001 a --workspace <dir> --session <SessionID>
    python tools/task.py set T001 --gsb-conclusion "A 更好" --gsb-reason-file reason.md
    python tools/task.py reset T001 --workspace <dir>
    python tools/task.py report T001
    python tools/task.py list
    python tools/task.py push [T001]

本机没装 Python 时用 uv 跑： uv run python tools/task.py ...
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys

import find_trajectory

try:  # Windows 控制台/管道默认可能是 cp936，统一按 utf-8 输出避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS)
TASKS = os.path.join(REPO, "tasks")
WORKSPACE_RECORD = ".workspace"  # 记在仓库里（不进工作区，避免给模型任何"这是评测题"的暗示）

# 一道题跑两轮，工作区按 A / B 两份摆放： <题目目录>/A  <题目目录>/B
SIDES = ("A", "B")

# 出好的 prompt 另存一份到桌面，方便双击打开、复制粘贴
PROMPT_DIR_NAME = "GSB_prompt"

# 题目工作区默认放这里： <桌面>/GSB_codex/projects/<题号>/
TASKS_DIR_NAME = os.environ.get("GSB_TASKS_DIR") or os.path.join("GSB_codex", "projects")

# A/B 项目目录与 Codex 启动器分开保存
LAUNCHERS_DIR_NAME = os.path.join("GSB_codex", "launchers")

# 每道题一个标签颜色，四个窗口一眼分得开
LAUNCH_COLORS = ["#8e44ad", "#d35400", "#16a085", "#c2185b",
                 "#2980b9", "#c0392b", "#27ae60", "#7f8c8d"]

# 跑题用的 Codex CLI 配置（codexcli 启动器里把 CODEX_HOME 指到这儿）
RUNS_DIR = "runs"

# 工作区里这些目录/文件属于"未跟踪的产物"，既不进快照也不参与重置
EXCLUDES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "out",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    "coverage",
    ".gradle",
    ".cache",
    ".idea",
}


def git(*args, cwd=REPO, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def remote_slug():
    url = git("remote", "get-url", "origin", check=False)
    if not url or "github.com" not in url:
        return None, None
    slug = url.rstrip("/")
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "ssh://git@github.com/",
    ):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    else:
        # 远端 URL 里带了用户名或端口，例如 https://someone@github.com/o/r
        match = re.search(r"github\.com[:/]+(.+)$", slug)
        if not match:
            return None, None
        slug = match.group(1)
    owner, _, name = slug.partition("/")
    if not owner or not name:
        return None, None
    return owner, name


def permalink(sha):
    owner, name = remote_slug()
    if not owner:
        return ""
    return f"https://github.com/{owner}/{name}/commit/{sha}"


def raw_url(sha, path):
    owner, name = remote_slug()
    if not owner:
        return ""
    return f"https://raw.githubusercontent.com/{owner}/{name}/{sha}/{path}"


def branches(task_id):
    key = task_id.lower()
    return {"base": f"{key}/base", "a": f"{key}/a", "b": f"{key}/b"}


def ref(branch):
    """分支引用：优先本地分支，退回 origin/<branch>。

    刚 clone 下来的仓库只有远端分支，直接写 t003/base 会解析不到。
    """
    if git("rev-parse", "--verify", "--quiet", branch, check=False):
        return branch
    remote = f"origin/{branch}"
    if git("rev-parse", "--verify", "--quiet", remote, check=False):
        return remote
    return ""


def task_dir(task_id):
    return os.path.join(TASKS, task_id.upper())


def load_meta(task_id):
    path = os.path.join(task_dir(task_id), "meta.json")
    if not os.path.exists(path):
        raise SystemExit(f"题目 {task_id} 不存在（缺 {path}）")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def desktop_dir():
    """桌面目录。Windows 上优先问系统（桌面可能被重定向），拿不到就退回 ~/Desktop。"""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                if buf.value and os.path.isdir(buf.value):
                    return buf.value
        except Exception:
            pass
    candidate = os.path.join(os.path.expanduser("~"), "Desktop")
    return candidate if os.path.isdir(candidate) else os.path.expanduser("~")


def prompt_copy_path(task_id, meta):
    """桌面副本的路径：<桌面>/prompt原文/<题目号>-<项目名>.txt"""
    title = (meta.get("title") or "").strip()
    slug = title.split()[0] if title else task_id.lower()
    return os.path.join(desktop_dir(), PROMPT_DIR_NAME, f"{task_id.upper()}-{slug}.txt")


def default_task_root(task_id):
    """题目目录的默认位置：<桌面>/GSB_codex/projects/<题号>/"""
    return os.path.join(desktop_dir(), TASKS_DIR_NAME, task_id.upper())


def launcher_dir():
    """Codex 启动器的独立目录。"""
    return os.environ.get("GSB_LAUNCHERS_DIR") or os.path.join(
        desktop_dir(), LAUNCHERS_DIR_NAME
    )


def drop_prompt_copy(task_id, meta=None):
    """把 prompt 原文复制一份到桌面，双击就能打开、全选复制。"""
    meta = meta or load_meta(task_id)
    src = os.path.join(task_dir(task_id), meta.get("prompt_file") or "prompt.md")
    if not os.path.exists(src):
        return ""
    dest = prompt_copy_path(task_id, meta)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def save_meta(task_id, meta):
    path = os.path.join(task_dir(task_id), "meta.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main_branch():
    for candidate in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", candidate, check=False):
            return candidate
    return "main"


def clear_worktree(keep=()):
    """清空仓库工作区（保留 .git 与 keep 中的顶层项）。"""
    for name in os.listdir(REPO):
        if name == ".git" or name in keep:
            continue
        path = os.path.join(REPO, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


def copy_workspace(src, dst=REPO):
    if not os.path.isdir(src):
        raise SystemExit(f"工作区不存在: {src}")
    for name in os.listdir(src):
        if name in EXCLUDES:
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*EXCLUDES))
        else:
            shutil.copy2(s, d)


def commit_all(message, cwd=REPO):
    git("add", "-A", cwd=cwd)
    if not git("status", "--porcelain", cwd=cwd):
        return git("rev-parse", "HEAD", cwd=cwd)
    git("commit", "-q", "-m", message, cwd=cwd)
    return git("rev-parse", "HEAD", cwd=cwd)


def ensure_workspace_repo(workspace):
    """工作区要是个 git 仓库：模型跑完可以自己提交产物，重置也能走 reset --hard。"""
    if not is_git_repo(workspace):
        git("init", "-q", "-b", "main", cwd=workspace)
        if not git("config", "user.name", cwd=workspace, check=False):
            name, email = default_git_identity()
            git("config", "user.name", name, cwd=workspace)
            git("config", "user.email", email, cwd=workspace)
        commit_all("initial environment", cwd=workspace)
        return True
    return False


def default_git_identity():
    """工作区自己的 git 身份：优先用全局配置，没有就给个中性占位。"""
    name = git("config", "--global", "user.name", check=False).strip()
    email = git("config", "--global", "user.email", check=False).strip()
    return name or "task-author", email or "task-author@example.com"


def is_git_repo(path):
    return bool(git("rev-parse", "--git-dir", cwd=path, check=False))


def workspace_git_reset(workspace):
    """把工作区 git 仓库硬重置到最初那次提交，并清掉未跟踪文件。"""
    if not is_git_repo(workspace):
        return False
    roots = git("rev-list", "--max-parents=0", "HEAD", cwd=workspace, check=False).split()
    if not roots:
        return False
    git("reset", "--hard", roots[-1], cwd=workspace)
    git("clean", "-fdx", cwd=workspace)
    return True


def workspace_store_path():
    return os.path.join(os.path.expanduser("~"), ".coding-agent-tasks", "workspaces.json")


def load_workspace_store():
    """读本机的工作区登记。旧格式（任务 -> 路径列表）会自动升级成新格式。"""
    path = workspace_store_path()
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            raw = {}
    store = {}
    for key, value in raw.items():
        if isinstance(value, list):                 # 旧格式
            store[key.upper()] = {"root": "", "workspaces": list(value)}
        elif isinstance(value, dict):
            store[key.upper()] = {
                "root": value.get("root", ""),
                "workspaces": list(value.get("workspaces", [])),
            }
    return store


def save_workspace_store(store):
    path = workspace_store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def write_workspace_record(task_id, workspace, root=""):
    """登记一个工作区。

    存在用户目录下、不入库：工作区路径是本机状态，而且建新题时会清空仓库工作区，
    记在 tasks/ 里会被一起清掉。
    """
    task = task_id.upper()
    store = load_workspace_store()
    entry = store.setdefault(task, {"root": "", "workspaces": []})
    target = os.path.abspath(workspace)
    if not any(same_path(item, target) for item in entry["workspaces"]):
        entry["workspaces"].append(target)
    if root:
        entry["root"] = os.path.abspath(root)
    save_workspace_store(store)


def task_root(task_id):
    """这道题的工作区根目录（下面应该有 A / B 两个子目录）。"""
    entry = load_workspace_store().get(task_id.upper()) or {}
    return entry.get("root", "")


def side_path(task_id, side, root=""):
    side = (side or "").upper()
    if side not in SIDES:
        raise SystemExit(f"--side 只能是 {' 或 '.join(SIDES)}")
    base_root = os.path.abspath(root) if root else task_root(task_id)
    if not base_root:
        raise SystemExit(
            f"{task_id} 还没登记工作区根目录。先跑一次："
            f" t prep {task_id.upper()} --root <题目目录>"
        )
    return os.path.join(base_root, side)


def read_workspace_records(task_id):
    records = []
    legacy = os.path.join(task_dir(task_id), WORKSPACE_RECORD)
    if os.path.exists(legacy):
        with open(legacy, encoding="utf-8") as fh:
            records.extend(line.strip() for line in fh if line.strip())
    entry = load_workspace_store().get(task_id.upper()) or {}
    records.extend(entry.get("workspaces", []))
    seen, out = set(), []
    for item in records:
        key = os.path.normcase(os.path.abspath(item))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def read_workspace_record(task_id):
    records = read_workspace_records(task_id)
    return records[-1] if records else ""


def same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def sync_workspace_to_branch(task_id, role, workspace):
    """把工作区内容做成代码分支（base/a/b）。"""
    br = branches(task_id)[role]
    base = branches(task_id)["base"]
    dirty = git("status", "--porcelain")
    if dirty:
        raise SystemExit(
            "仓库里有未提交的改动，先提交再执行（本操作会清空工作区）：\n" + dirty
        )
    if role == "base":
        git("checkout", "--orphan", br)
        git("rm", "-rf", "--cached", "-q", ".", check=False)
    else:
        start = ref(base)
        if not start:
            raise SystemExit(f"缺少初始快照分支 {base}，请先执行 new")
        git("checkout", "-B", br, start)
    clear_worktree()
    copy_workspace(workspace)
    sha = commit_all(f"{task_id.upper()} {role} ({dt.date.today().isoformat()})")
    return br, sha


# ---------------------------------------------------------------- commands


def cmd_new(args):
    task_id = args.id.upper()
    dest = task_dir(task_id)
    if os.path.exists(dest):
        raise SystemExit(f"{dest} 已存在")
    with open(args.prompt_file, encoding="utf-8") as fh:
        prompt = fh.read()

    if ensure_workspace_repo(args.workspace):
        print(f"已把工作区初始化为 git 仓库: {args.workspace}")

    # 先做代码分支：这一步会清空仓库工作区，台账文件必须等它之后再写
    original = git("rev-parse", "--abbrev-ref", "HEAD")
    br, sha = sync_workspace_to_branch(task_id, "base", args.workspace)
    git("checkout", "-q", original)

    template = os.path.join(TASKS, "_TEMPLATE")
    shutil.copytree(template, dest)
    with open(os.path.join(dest, "prompt.md"), "w", encoding="utf-8") as fh:
        fh.write(prompt if prompt.endswith("\n") else prompt + "\n")
    os.makedirs(os.path.join(dest, "trajectories"), exist_ok=True)

    meta = {
        "task_id": task_id,
        "title": args.title,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "task_type": args.task_type,
        "difficulty": args.difficulty,
        "language_framework": args.lang,
        "harness": args.harness,
        "harness_version": args.harness_version,
        "os": args.os,
        "env_level": args.env_level,
        "prompt_file": "prompt.md",
        "initial_snapshot": {"branch": br, "sha": sha, "permalink": permalink(sha)},
        "runs": {
            "A": {"session_id": "", "trajectory_local": "", "trajectory_url": "",
                  "branch": branches(task_id)["a"], "product_snapshot_sha": "",
                  "product_snapshot_permalink": ""},
            "B": {"session_id": "", "trajectory_local": "", "trajectory_url": "",
                  "branch": branches(task_id)["b"], "product_snapshot_sha": "",
                  "product_snapshot_permalink": ""},
        },
        "notes": args.notes,
    }
    save_meta(task_id, meta)
    write_workspace_record(task_id, args.workspace)
    commit_all(f"{task_id} metadata")
    desktop_copy = drop_prompt_copy(task_id, meta)

    print(f"初始环境快照: {sha}")
    print(f"  branch    : {br}")
    print(f"  permalink : {permalink(sha)}")
    if desktop_copy:
        print(f"prompt 副本: {desktop_copy}")

    # 默认就把 A / B 两份工作区和启动器一起铺好（不传 --root 就放桌面 \GSB题目\<题号>）
    print()
    cmd_prep(argparse.Namespace(id=task_id, root=args.root, fresh=False))
    if args.push:
        cmd_push(argparse.Namespace(id=task_id))
    return 0


def cmd_record(args):
    task_id = args.id.upper()
    role = args.role.lower()
    meta = load_meta(task_id)
    workspace = resolve_workspace(args, task_id)
    base = branches(task_id)["base"]
    if not ref(base):
        raise SystemExit(f"缺少初始快照分支 {base}，请先执行 new")
    if not git("rev-parse", "--verify", "--quiet", meta["initial_snapshot"]["sha"], check=False):
        raise SystemExit("初始环境快照 commit 在当前仓库里找不到")

    original = git("rev-parse", "--abbrev-ref", "HEAD")
    br, sha = sync_workspace_to_branch(task_id, role, workspace)
    git("checkout", "-q", original)

    hits = find_trajectory.find(args.session)
    local_path = ""
    source_path = ""
    if hits:
        src = hits[0]["path"]
        source_path = src
        traj_dir = os.path.join(task_dir(task_id), "trajectories")
        os.makedirs(traj_dir, exist_ok=True)
        local_path = os.path.join(traj_dir, f"{role.upper()}-{args.session}.jsonl")
        shutil.copy2(src, local_path)

    info = meta["runs"][role.upper()]
    info.update({
        "session_id": args.session,
        "branch": br,
        "product_snapshot_sha": sha,
        "product_snapshot_permalink": permalink(sha),
        "trajectory_local": local_path,
        "trajectory_source": source_path,
    })
    save_meta(task_id, meta)
    commit_all(f"{task_id.upper()} record {role.upper()} ({args.session})")

    if local_path:
        rel = os.path.relpath(local_path, REPO).replace("\\", "/")
        info["trajectory_url"] = raw_url(git("rev-parse", "HEAD"), rel)
        save_meta(task_id, meta)
        commit_all(f"{task_id.upper()} trajectory url {role.upper()}")

    print(f"{role.upper()} 产物快照: {sha}")
    print(f"  branch    : {br}")
    if hits:
        print(f"  轨迹文件  : {hits[0]['path']}")
    else:
        print(f"  轨迹文件  : 未找到 SessionID {args.session} 的 jsonl，请确认 SessionID 或手动放入 trajectories/")
    if args.push:
        cmd_push(argparse.Namespace(id=task_id))
    return 0


def cmd_set(args):
    """补/改题目元数据：Harness 版本、GSB 结论与理由、录屏路径等。"""
    task_id = args.id.upper()
    meta = load_meta(task_id)
    changed = []

    for attr, key in (
        ("title", "title"),
        ("task_type", "task_type"),
        ("difficulty", "difficulty"),
        ("lang", "language_framework"),
        ("harness", "harness"),
        ("harness_version", "harness_version"),
        ("os", "os"),
        ("env_level", "env_level"),
        ("validity", "validity"),
        ("remark", "remark"),
    ):
        value = getattr(args, attr)
        if value is not None:
            meta[key] = value
            changed.append(key)

    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as fh:
            prompt = fh.read()
        if not prompt.endswith("\n"):
            prompt += "\n"
        with open(os.path.join(task_dir(task_id), "prompt.md"), "w", encoding="utf-8") as fh:
            fh.write(prompt)
        changed.append("prompt.md")

    if args.gsb_conclusion is not None or args.gsb_reason_file:
        gsb = meta.setdefault("gsb", {})
        if args.gsb_conclusion is not None:
            gsb["conclusion"] = args.gsb_conclusion
            changed.append("gsb.conclusion")
        if args.gsb_reason_file:
            with open(args.gsb_reason_file, encoding="utf-8") as fh:
                gsb["reason"] = fh.read().strip()
            gsb["reason_author"] = args.reason_author
            changed.append("gsb.reason")

    for role, path in (("A", args.a_recording), ("B", args.b_recording)):
        if path:
            meta["runs"][role]["recording_local"] = os.path.abspath(path)
            changed.append(f"runs.{role}.recording_local")

    # 轨迹 jsonl 不在本机时（例如跑在另一台机器上），至少要能把 SessionID 记下来
    for role, sid in (("A", args.a_session), ("B", args.b_session)):
        if sid:
            meta["runs"][role]["session_id"] = sid.strip()
            changed.append(f"runs.{role}.session_id")

    # 轨迹是在另一台机器上跑的：把那边导出的 jsonl 拷进仓库，report 里就是可点击的本机文件
    for role, src in (("A", args.a_trajectory), ("B", args.b_trajectory)):
        if not src:
            continue
        if not os.path.exists(src):
            raise SystemExit(f"找不到轨迹文件 {src}")
        sid = (meta["runs"][role].get("session_id") or "").strip()
        name = f"{role}-{sid}.jsonl" if sid else f"{role}-{os.path.basename(src)}"
        traj_dir = os.path.join(task_dir(task_id), "trajectories")
        os.makedirs(traj_dir, exist_ok=True)
        dest = os.path.join(traj_dir, name)
        shutil.copy2(src, dest)
        meta["runs"][role]["trajectory_local"] = dest
        meta["runs"][role]["trajectory_source"] = os.path.abspath(src)
        changed.append(f"runs.{role}.trajectory_local")

    if not changed:
        raise SystemExit("没有要改的内容；用 python tools/task.py set --help 看可用参数")

    meta["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_meta(task_id, meta)
    commit_all(f"{task_id} set: {', '.join(changed)}")
    print(f"已更新 {task_id}: {', '.join(changed)}")
    if args.prompt_file:
        print(f"prompt 副本已同步: {drop_prompt_copy(task_id, meta)}")
    if args.push:
        cmd_push(argparse.Namespace(id=task_id))
    return 0


def launch_color(task_id):
    digits = re.sub(r"\D", "", task_id)
    index = (int(digits) - 1) if digits else 0
    return LAUNCH_COLORS[index % len(LAUNCH_COLORS)]


def codex_cli_config_path():
    """跑题用的那份 codexcli 配置。"""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex-cli")
    return os.path.join(home, "config.toml")


def trust_project(path):
    """把目录加进 codexcli 的信任列表，免得每次开窗口都弹「是否信任此目录」。

    Codex CLI 的信任是逐目录精确匹配的，所以每道题的 A / B 都要各写一条。
    """
    cfg = codex_cli_config_path()
    if not os.path.exists(cfg):
        return False
    key = os.path.abspath(path).lower().replace("/", "\\")
    with open(cfg, encoding="utf-8") as fh:
        text = fh.read()
    if f"[projects.'{key}']" in text:
        return False
    backup = cfg + ".bak-before-trust"
    if not os.path.exists(backup):
        shutil.copy2(cfg, backup)
    with open(cfg, "a", encoding="utf-8") as fh:
        fh.write(f"\n[projects.'{key}']\ntrust_level = \"trusted\"\n")
    return True


def workspace_dirty(workspace):
    """工作区里是不是有初始环境之外的东西（跑过一轮就会有）。"""
    if not os.path.isdir(workspace):
        return False
    if not is_git_repo(workspace):
        return bool([n for n in os.listdir(workspace) if n not in EXCLUDES])
    status = git("status", "--porcelain", cwd=workspace, check=False)
    return bool(status.strip())


def hard_clean_workspace(workspace):
    """原地把工作区清回最初提交：文件、未跟踪产物、模型留下的分支/stash/reflog 一起清。

    不删除目录本身 —— 窗口开着的时候目录被占用，删目录会 PermissionError，
    而且删一半会把正在跑的那一轮弄坏。
    """
    if not is_git_repo(workspace):
        return ""
    roots = git("rev-list", "--max-parents=0", "HEAD", cwd=workspace, check=False).split()
    if not roots:
        return ""
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace, check=False).strip()
    git("reset", "--hard", roots[-1], cwd=workspace, check=False)
    git("clean", "-fdx", cwd=workspace, check=False)
    for ref in git("for-each-ref", "--format=%(refname:short)", "refs/heads",
                   cwd=workspace, check=False).splitlines():
        ref = ref.strip()
        if ref and ref != current:
            git("branch", "-D", ref, cwd=workspace, check=False)
    git("stash", "clear", cwd=workspace, check=False)
    git("reflog", "expire", "--expire=now", "--all", cwd=workspace, check=False)
    git("gc", "--prune=now", "--quiet", cwd=workspace, check=False)
    return "原地重置到最初提交"


def archive_run(task_id, side, workspace):
    """把这一轮跑出来的东西原样留一份，之后工作区才能放心重置。"""
    label = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    # 存到本机用户目录，不进仓库：失败产物不该被推到 GitHub，也少一个模型可能翻到的地方
    out = os.path.join(os.path.expanduser("~"), ".coding-agent-tasks", RUNS_DIR,
                       task_id.upper(), f"{side}-{label}")
    os.makedirs(out, exist_ok=True)
    copied = 0
    for name in os.listdir(workspace):
        if name in EXCLUDES or name == ".git":
            continue
        src = os.path.join(workspace, name)
        dst = os.path.join(out, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*EXCLUDES))
        else:
            shutil.copy2(src, dst)
        copied += 1
    if not copied:
        shutil.rmtree(out, ignore_errors=True)
        return ""
    return out


def cmd_cycle(args):
    """开窗口前用的：上一轮的残留先存档，然后把工作区**整个清掉重新铺**。"""
    task_id = args.id.upper()
    workspace = resolve_workspace(args, task_id)
    side = (getattr(args, "side", "") or os.path.basename(workspace)).upper()
    if side not in SIDES:
        side = "X"
    base_ref = ref(branches(task_id)["base"])
    if not base_ref:
        raise SystemExit(f"缺少初始快照分支 {branches(task_id)['base']}")

    archived = ""
    if workspace_dirty(workspace):
        archived = archive_run(task_id, side, workspace)
        if archived:
            print(f"上一轮的东西先存了一份: {archived}")

    # 原地清干净（不删目录本身）：窗口开着的时候目录被占用，删目录会失败；
    # 而且这样连模型在 .git 里留下的提交、分支、stash、reflog 也一起清掉
    how = hard_clean_workspace(workspace)
    if not how:
        how = materialize(task_id, base_ref, workspace)
    write_workspace_record(task_id, workspace, root=task_root(task_id))
    if trust_project(workspace):
        print(f"已把 {workspace} 加进 codexcli 信任列表（以后不再问）")

    added, missing, changed = compare_with_base(base_ref, workspace)
    if added or missing or changed:
        how = materialize(task_id, base_ref, workspace)
        added, missing, changed = compare_with_base(base_ref, workspace)
    print(f"{side} 工作区已就绪: {workspace}   [{how}]")
    if added or missing or changed:
        print("  注意：和初始环境对不上，请把下面这段发我")
        for label, items in (("多出来的", added), ("少了", missing), ("内容不同", changed)):
            for item in items[:10]:
                print(f"    {label}: {item}")
    else:
        print("  已核验：与初始环境逐文件一致，没有任何残留")
    return 0


def compare_with_base(base_ref, workspace):
    """把工作区内容和初始快照逐文件比一遍，返回（多的、少的、内容不同的）。"""
    want = {}
    for line in git("ls-tree", "-r", base_ref).splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        want[path] = meta.split()[2]

    have = {}
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, workspace).replace("\\", "/")
            have[rel] = git("hash-object", "--", full, cwd=workspace, check=False)

    added = sorted(set(have) - set(want))
    missing = sorted(set(want) - set(have))
    changed = sorted(p for p in set(have) & set(want) if have[p] != want[p])
    return added, missing, changed


def write_launchers(task_id, root, meta):
    """在独立启动器目录写 A/B 启动器，项目目录只保存 A/B 工作区。"""
    slug = ((meta.get("title") or "").strip().split() or [task_id.lower()])[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "", slug) or task_id.lower()
    color = launch_color(task_id)
    written = []
    launch_root = launcher_dir()
    os.makedirs(launch_root, exist_ok=True)

    for side in SIDES:
        label = f"{task_id.upper()}-{side} {slug}"
        path = os.path.join(launch_root, f"{task_id.upper()}-{side}-run.cmd")
        workspace = os.path.abspath(os.path.join(root, side))
        # .cmd 必须是纯 ASCII：cmd.exe 按 OEM 代码页解析文件，中文注释会出乱码
        body = (
            "@echo off\r\n"
            f"rem {label} -- open a named/colored terminal tab in this round's workspace\r\n"
            "setlocal\r\n"
            f'set "LABEL={label}"\r\n'
            f'set "COLOR={color}"\r\n'
            f'set "DIR={workspace}"\r\n'
            f'set "TASK={task_id.upper()}"\r\n'
            f'set "SIDE={side}"\r\n'
            f'set "REPO={REPO}"\r\n'
            "rem always start from a clean initial environment: the previous attempt is\r\n"
            "rem archived under tasks\\<ID>\\runs\\ first, so nothing is ever lost\r\n"
            'echo [launcher] preparing %TASK%-%SIDE% ...\r\n'
            'pushd "%REPO%"\r\n'
            "uv run python tools\\task.py cycle %TASK% --side %SIDE%\r\n"
            "popd\r\n"
            "where wt >nul 2>nul\r\n"
            "if errorlevel 1 goto plain\r\n"
            'wt -w 0 nt --title "%LABEL%" --tabColor "%COLOR%" '
            '--suppressApplicationTitle -d "%DIR%" cmd /k "%USERPROFILE%\\.codex-cli-relay\\bin\\codex.cmd" --model "auto_model/urm" --yolo\r\n'
            "if errorlevel 1 goto plain\r\n"
            "exit /b 0\r\n"
            ":plain\r\n"
            "title %LABEL%\r\n"
            'cd /d "%DIR%"\r\n'
            '"%USERPROFILE%\\.codex-cli-relay\\bin\\codex.cmd" --model "auto_model/urm" --yolo\r\n'
        )
        with open(path, "w", encoding="ascii", errors="replace", newline="") as fh:
            fh.write(body)
        written.append(path)
    return written


def cmd_launch(args):
    """给题目目录生成 A / B 启动器（不动工作区内容）。"""
    if args.id:
        ids = [args.id.upper()]
    else:
        store = load_workspace_store()
        ids = sorted(t for t, entry in store.items() if entry.get("root"))
    if not ids:
        print("没有登记过工作区根目录的题目，先用 t prep <题号> --root <目录>")
        return 0

    for task_id in ids:
        meta = load_meta(task_id)
        root = args.root or task_root(task_id)
        if not root:
            print(f"  {task_id}: 跳过（没登记根目录）")
            continue
        for path in write_launchers(task_id, root, meta):
            print(f"  {os.path.basename(path)}  ->  {path}")
        for side in SIDES:
            trust_project(os.path.join(root, side))
    return 0


def write_rebuild_script(destination, task_ids):
    """在独立启动器目录里放一个一键重建脚本。"""
    path = os.path.join(destination, "重建全部题目.cmd")
    body = (
        "@echo off\r\n"
        "rem Rebuild every task workspace (A/B) from git, then refresh the launchers.\r\n"
        "rem Use this after the folders were deleted or after moving to another machine.\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{REPO}"\r\n'
        "echo Rebuilding task workspaces from git ...\r\n"
        "uv run python tools\\task.py rebuild\r\n"
        "echo.\r\n"
        "pause\r\n"
    )
    with open(path, "w", encoding="ascii", errors="replace", newline="") as fh:
        fh.write(body)
    return path


def cmd_rebuild(args):
    """按登记的根目录，把所有题目的 A / B 工作区重新铺一遍，并更新启动器。

    桌面上误删了文件夹、或者换了机器之后，跑这一条就全回来了（内容来自 git 分支，不会丢）。
    """
    store = load_workspace_store()
    ids = [args.id.upper()] if args.id else sorted(t for t, e in store.items() if e.get("root"))
    if not ids:
        print("没有登记过工作区根目录的题目")
        return 0

    for task_id in ids:
        root = task_root(task_id)
        if not root:
            print(f"{task_id}: 跳过（没登记根目录）")
            continue
        meta = load_meta(task_id)
        base = branches(task_id)["base"]
        base_ref = ref(base)
        if not base_ref:
            print(f"{task_id}: 跳过（找不到 {base} 分支）")
            continue
        print(f"{task_id}  ->  {root}")
        os.makedirs(root, exist_ok=True)
        for side in SIDES:
            dest = os.path.join(root, side)
            how = materialize(task_id, base_ref, dest)
            write_workspace_record(task_id, dest, root=root)
            trust_project(dest)
            print(f"    {side}: {how}")
        write_launchers(task_id, root, meta)
    launch_root = launcher_dir()
    os.makedirs(launch_root, exist_ok=True)
    if ids:
        print(f"    一键重建脚本: {write_rebuild_script(launch_root, ids)}")
    print("\n完成。请到桌面 GSB_codex\\launchers 双击 <题号>-A-run.cmd / <题号>-B-run.cmd。")
    return 0


def cmd_prompt(args):
    """把 prompt 原文同步到桌面的「prompt原文」文件夹。"""
    if args.id:
        ids = [args.id.upper()]
    elif os.path.isdir(TASKS):
        ids = sorted(name for name in os.listdir(TASKS)
                     if not name.startswith("_") and os.path.isdir(os.path.join(TASKS, name)))
    else:
        ids = []
    if not ids:
        print("还没有题目")
        return 0

    folder = ""
    for task_id in ids:
        try:
            dest = drop_prompt_copy(task_id)
        except SystemExit as exc:
            print(f"  {task_id}: 跳过（{exc}）")
            continue
        if dest:
            folder = os.path.dirname(dest)
            print(f"  {task_id} -> {os.path.basename(dest)}")
    if folder:
        print(f"\n桌面文件夹: {folder}")
    return 0


def wipe_side(path):
    """清空一个副本目录。只允许清 A / B 这种按约定命名的目录，避免误删。"""
    target = os.path.abspath(path)
    if os.path.basename(target).upper() not in SIDES:
        raise SystemExit(
            f"拒绝清空 {target}：只清 <题目目录>\\A 或 \\B 这种按约定命名的副本。"
        )
    if not os.path.isdir(target):
        return

    def force_remove(func, path, _exc):
        # Windows 上 git 的对象文件是只读的，直接 rmtree 会 PermissionError
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass
        func(path)

    try:
        shutil.rmtree(target, onexc=force_remove)
    except TypeError:                      # Python < 3.12 没有 onexc
        shutil.rmtree(target, onerror=force_remove)


def materialize(task_id, base_ref, dest):
    """把初始快照铺到 dest，并把 dest 初始化成 git 仓库（下次重置走 reset --hard）。"""
    base = branches(task_id)["base"]
    os.makedirs(dest, exist_ok=True)
    if workspace_git_reset(dest):
        return "git reset --hard"

    tracked = {line for line in git("ls-tree", "-r", "--name-only", base_ref).splitlines() if line}

    # 1) 删掉不属于初始快照的文件（node_modules / venv / 构建产物等一并清掉）
    for dirpath, _dirnames, filenames in os.walk(dest, topdown=False):
        rel = os.path.relpath(dirpath, dest).replace("\\", "/")
        rel = "" if rel == "." else rel
        if rel == ".git" or rel.startswith(".git/"):
            continue
        for name in filenames:
            full = f"{rel}/{name}" if rel else name
            if full not in tracked:
                os.remove(os.path.join(dirpath, name))
        if rel:
            still_needed = any(t == rel or t.startswith(rel + "/") for t in tracked)
            if not still_needed:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

    # 2) 用初始快照的内容覆盖回去
    archive = subprocess.run(["git", "archive", base_ref], cwd=REPO, capture_output=True)
    if archive.returncode != 0:
        raise SystemExit("git archive 失败")
    extract = subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, capture_output=True)
    if extract.returncode != 0:
        raise SystemExit(f"解包失败: {extract.stderr.decode(errors='replace')[:300]}")

    ensure_workspace_repo(dest)
    return f"铺出 {base}"


def resolve_workspace(args, task_id, need=True):
    """--side 优先（走后缀约定），否则用 --workspace。"""
    if getattr(args, "side", None):
        return side_path(task_id, args.side, getattr(args, "root", "") or "")
    workspace = getattr(args, "workspace", None)
    if workspace:
        return os.path.abspath(workspace)
    if need:
        raise SystemExit("要么给 --side a|b（走 <题目目录>\\A|B 约定），要么给 --workspace <目录>")
    return ""


def cmd_prep(args):
    """按 A / B 约定铺出这道题的两份工作区（已存在就重置）。"""
    task_id = args.id.upper()
    load_meta(task_id)
    base = branches(task_id)["base"]
    base_ref = ref(base)
    if not base_ref:
        raise SystemExit(f"缺少初始快照分支 {base}")
    root = os.path.abspath(args.root or default_task_root(task_id))
    os.makedirs(root, exist_ok=True)

    print(f"{task_id} 工作区根目录: {root}")
    for side in SIDES:
        dest = os.path.join(root, side)
        if args.fresh and os.path.isdir(dest):
            wipe_side(dest)
        how = materialize(task_id, base_ref, dest)
        write_workspace_record(task_id, dest, root=root)
        print(f"  {side}: {dest}   （{how}）")

    print(f"\n两轮分别在这两个目录里跑，用完全相同的 prompt：")
    print(f"  A 窗口: cd \"{os.path.join(root, 'A')}\"")
    print(f"  B 窗口: cd \"{os.path.join(root, 'B')}\"")
    for side in SIDES:
        trust_project(os.path.join(root, side))
    for path in write_launchers(task_id, root, load_meta(task_id)):
        print(f"  启动器 : {path}")
    print(f"跑完记账： t record {task_id} a --side a --session <A-SessionID>")
    print(f"         t record {task_id} b --side b --session <B-SessionID>")
    return 0


def cmd_reset(args):
    task_id = args.id.upper()
    workspace = resolve_workspace(args, task_id)
    recorded = read_workspace_records(task_id)
    if recorded and not args.force and not any(
        same_path(item, workspace) for item in recorded
    ):
        raise SystemExit(
            f"拒绝操作：{task_id} 记录的工作区是 {', '.join(recorded)}，"
            f"与你传入的 {workspace} 不一致，已中止。\n"
            f"如果这是同一道题的另一份副本，加 --force 继续。"
        )

    if args.fresh:
        wipe_side(workspace)

    base = branches(task_id)["base"]
    base_ref = ref(base)
    if not base_ref:
        raise SystemExit(f"缺少初始快照分支 {base}，无法重置")

    how = materialize(task_id, base_ref, workspace)
    write_workspace_record(task_id, workspace, root=args.root or task_root(task_id))
    print(f"{os.path.basename(workspace)} 已重置到初始环境 {base} "
          f"({load_meta(task_id)['initial_snapshot']['sha']})   [{how}]")
    print("未跟踪文件已清掉，可以直接重跑这一轮。")
    return 0


def cmd_report(args):
    if not args.id:
        ids = [name for name in sorted(os.listdir(TASKS))
               if not name.startswith("_") and os.path.isdir(os.path.join(TASKS, name))] \
            if os.path.isdir(TASKS) else []
        if not ids:
            print("还没有题目")
            return 0
        for i, name in enumerate(ids):
            if i:
                print("\n" + "-" * 72 + "\n")
            cmd_report(argparse.Namespace(id=name))
        return 0

    task_id = args.id.upper()
    meta = load_meta(task_id)
    init = meta["initial_snapshot"]
    a, b = meta["runs"]["A"], meta["runs"]["B"]
    gsb = meta.get("gsb") or {}

    def local_link(entry, label):
        out = []
        source = entry.get("trajectory_source") or ""
        local = entry.get("trajectory_local") or ""
        if source and os.path.exists(source):
            out.append(f"{label}（原始文件）: [{os.path.basename(source)}]({source})")
        if local and os.path.exists(local):
            out.append(f"{label}（仓库副本）: [{os.path.basename(local)}]({local})")
        return out or [f"{label}: （未找到本地文件）"]

    lines = [
        f"题目: {task_id} {meta.get('title') or ''}",
        f"任务类型: {meta.get('task_type')}",
        f"任务难度: {meta.get('difficulty')}",
        f"语言/框架: {meta.get('language_framework')}",
        f"Harness: {meta.get('harness')} {meta.get('harness_version')}",
        f"操作系统: {meta.get('os')}",
        f"环境可复现等级: {meta.get('env_level')}",
        f"初始环境快照: {init.get('permalink') or init.get('sha')}",
        f"A-SessionID: {a.get('session_id')}",
        f"A-轨迹文件: {a.get('trajectory_url') or '(待上传)'}",
        f"A-产物快照: {a.get('product_snapshot_permalink') or a.get('product_snapshot_sha')}",
        f"B-SessionID: {b.get('session_id')}",
        f"B-轨迹文件: {b.get('trajectory_url') or '(待上传)'}",
        f"B-产物快照: {b.get('product_snapshot_permalink') or b.get('product_snapshot_sha')}",
        f"GSB 结论: {gsb.get('conclusion') or ''}",
        f"GSB 理由: {len(gsb.get('reason') or '')} 字",
        *([f"          （作者：{gsb.get('reason_author')}）"] if gsb.get("reason_author") else []),
        f"有效性: {meta.get('validity') or '有效'}",
        f"备注: {meta.get('remark') or ''}",
    ]
    print("\n".join(lines))
    print("\n--- 轨迹文件（点一下直接打开本机文件） ---")
    for line in local_link(a, "A-轨迹文件") + local_link(b, "B-轨迹文件"):
        print(line)

    for role, entry in (("A", a), ("B", b)):
        rec = entry.get("recording_local") or ""
        if rec and os.path.exists(rec):
            print(f"{role}-运行录屏: [{os.path.basename(rec)}]({rec})")
    return 0


def cmd_list(_args):
    if not os.path.isdir(TASKS):
        print("还没有题目")
        return 0
    for name in sorted(os.listdir(TASKS)):
        if name.startswith("_") or not os.path.isdir(os.path.join(TASKS, name)):
            continue
        try:
            meta = load_meta(name)
        except SystemExit:
            continue
        a, b = meta["runs"]["A"], meta["runs"]["B"]
        state = "初始快照已建" if meta["initial_snapshot"]["sha"] else "未建快照"
        state += " | A✓" if a["product_snapshot_sha"] else " | A-"
        state += " | B✓" if b["product_snapshot_sha"] else " | B-"
        print(f"{meta['task_id']:6s} {state:32s} {meta.get('language_framework','')}  {meta.get('title','')}")
    return 0


def cmd_push(args):
    task_id = (args.id or "").upper()
    if task_id:
        refs = [
            branch
            for branch in branches(task_id).values()
            if git("rev-parse", "--verify", "--quiet", branch, check=False)
        ]
        refs.append("main")
    else:
        refs = ["--all"]
    result = subprocess.run(["git", "push", "-u", "origin", *refs], cwd=REPO,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(result.stdout.strip() or result.stderr.strip())
    if result.returncode != 0:
        print("\n推送失败（多为网络问题）。稍后可重试： python tools/task.py push "
              + (task_id or ""))
        return 1
    owner, name = remote_slug()
    print(f"https://github.com/{owner}/{name}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Coding Agent 题目台账")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="新建题目并提交初始环境快照")
    n.add_argument("id")
    n.add_argument("--workspace", required=True)
    n.add_argument("--prompt-file", required=True)
    n.add_argument("--title", default="")
    n.add_argument("--task-type", dest="task_type", default="")
    n.add_argument("--difficulty", default="困难")
    n.add_argument("--lang", default="")
    n.add_argument("--harness", default="Codex CLI")
    n.add_argument("--harness-version", dest="harness_version", default="")
    n.add_argument("--os", default="Windows")
    n.add_argument("--env-level", dest="env_level", default="")
    n.add_argument("--notes", default="")
    n.add_argument("--root", help=f"题目目录；默认 {TASKS_DIR_NAME}\\<题号>（在桌面）")
    n.add_argument("--no-push", dest="push", action="store_false")
    n.set_defaults(push=True, func=cmd_new)

    r = sub.add_parser("record", help="记录 A/B 的产物快照与轨迹")
    r.add_argument("id")
    r.add_argument("role", choices=["a", "b"])
    r.add_argument("--workspace")
    r.add_argument("--side", help="a 或 b：用 <题目目录>\\A|B 约定定位工作区")
    r.add_argument("--root", help="工作区根目录（默认用本机登记过的）")
    r.add_argument("--session", required=True)
    r.add_argument("--no-push", dest="push", action="store_false")
    r.set_defaults(push=True, func=cmd_record)

    s = sub.add_parser("reset", help="把工作区重置回初始环境")
    s.add_argument("id")
    s.add_argument("--workspace")
    s.add_argument("--side", help="a 或 b：只重置这一轮的工作区")
    s.add_argument("--root", help="工作区根目录（默认用本机登记过的）")
    s.add_argument("--fresh", action="store_true",
                   help="先整个清空 A / B 目录再重铺（某轮跑废了用这个）")
    s.add_argument("--force", action="store_true",
                   help="允许铺到这道题的另一份工作区（A/B 并行跑时用）")
    s.set_defaults(func=cmd_reset)

    pr = sub.add_parser("prep", help="按 A / B 约定铺出这道题的两份工作区")
    pr.add_argument("id")
    pr.add_argument("--root", help=f"题目目录；默认桌面\\{TASKS_DIR_NAME}\\<题号>")
    pr.add_argument("--fresh", action="store_true", help="先清空再重铺")
    pr.set_defaults(func=cmd_prep)

    pm = sub.add_parser("prompt", help="把 prompt 原文同步到桌面「prompt原文」文件夹")
    pm.add_argument("id", nargs="?", help="题号；不给就同步全部")
    pm.set_defaults(func=cmd_prompt)

    lc = sub.add_parser("launch", help="生成 A/B 启动器（带标签名字和颜色的终端）")
    lc.add_argument("id", nargs="?", help="题号；不给就处理所有已登记的题目")
    lc.add_argument("--root", help="题目目录；默认用本机登记过的")
    lc.set_defaults(func=cmd_launch)

    cy = sub.add_parser("cycle", help="开窗口前准备：上一轮存档，工作区重置成全新")
    cy.add_argument("id")
    cy.add_argument("--side", help="a 或 b")
    cy.add_argument("--workspace")
    cy.add_argument("--root")
    cy.set_defaults(func=cmd_cycle)

    rb = sub.add_parser("rebuild", help="按登记重新铺出所有题目的 A / B 工作区与启动器")
    rb.add_argument("id", nargs="?", help="题号；不给就全部")
    rb.set_defaults(func=cmd_rebuild)

    st = sub.add_parser("set", help="补/改题目的元数据（Harness 版本、GSB、录屏等）")
    st.add_argument("id")
    st.add_argument("--title")
    st.add_argument("--task-type", dest="task_type")
    st.add_argument("--difficulty")
    st.add_argument("--lang")
    st.add_argument("--harness")
    st.add_argument("--harness-version", dest="harness_version")
    st.add_argument("--os")
    st.add_argument("--env-level", dest="env_level")
    st.add_argument("--validity", help="有效 / 作废-工程故障 / 作废-环境未重置 / 作废-其他")
    st.add_argument("--remark")
    st.add_argument("--prompt-file", dest="prompt_file")
    st.add_argument("--gsb-conclusion", dest="gsb_conclusion", help="A 更好 / Same / B 更好")
    st.add_argument("--gsb-reason-file", dest="gsb_reason_file",
                    help="人写好的 GSB 理由文件（本项目禁止 AI 代写）")
    st.add_argument("--reason-author", dest="reason_author", help="理由作者，默认本人")
    st.add_argument("--a-recording", dest="a_recording")
    st.add_argument("--b-recording", dest="b_recording")
    st.add_argument("--a-session", dest="a_session", help="A 的 SessionID（轨迹不在本机时用）")
    st.add_argument("--b-session", dest="b_session", help="B 的 SessionID")
    st.add_argument("--a-trajectory", dest="a_trajectory",
                    help="从另一台机器导出的 A 轨迹 jsonl，拷进仓库")
    st.add_argument("--b-trajectory", dest="b_trajectory",
                    help="从另一台机器导出的 B 轨迹 jsonl，拷进仓库")
    st.add_argument("--no-push", dest="push", action="store_false")
    st.set_defaults(push=True, func=cmd_set)

    rep = sub.add_parser("report", help="输出提交表字段")
    rep.add_argument("id", nargs="?", help="题号；不给就输出所有题目")
    rep.set_defaults(func=cmd_report)

    ls = sub.add_parser("list", help="列出所有题目")
    ls.set_defaults(func=cmd_list)

    pu = sub.add_parser("push", help="推送分支与台账")
    pu.add_argument("id", nargs="?")
    pu.set_defaults(func=cmd_push)
    return p


if __name__ == "__main__":
    sys.path.insert(0, TOOLS)
    arguments = build_parser().parse_args()
    sys.exit(arguments.func(arguments) or 0)
