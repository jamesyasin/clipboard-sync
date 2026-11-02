"""安全起子进程：一律走 posix_spawn，不用 fork()。

背景：本程序是「AppKit(ObjC runtime + libdispatch) + 多条工作线程」的进程。
在 macOS 上，这种进程里 fork() 出来的子进程在 exec 之前只要碰到一点 ObjC / dispatch
就会 abort（经典报错：*** multi-threaded process forked ***），严重时父进程一起崩。
os.posix_spawn 直接走 vfork/clone + exec，后台线程里调用也安全，是官方推荐做法。
"""
import os
import time

_SHELL_WRAP = "cd -- \"$1\" && shift && exec \"$@\""


def spawn_capture(cmd, timeout=60, cwd=None, env=None):
    """跑 cmd，返回 (returncode, stdout, stderr)。"""
    import tempfile

    out_fd, out_path = tempfile.mkstemp(prefix="cs_out_")
    err_fd, err_path = tempfile.mkstemp(prefix="cs_err_")
    pid = None
    try:
        if cwd:                      # posix_spawn 不支持 cwd，用 sh 包一层
            argv = ["/bin/sh", "-c", _SHELL_WRAP, "sh", str(cwd)] + [str(c) for c in cmd]
            exe = "/bin/sh"
        else:
            argv = [str(c) for c in cmd]
            exe = argv[0]
        fa = [(os.POSIX_SPAWN_DUP2, out_fd, 1), (os.POSIX_SPAWN_DUP2, err_fd, 2)]
        pid = os.posix_spawn(exe, argv, env or os.environ, file_actions=fa)
        os.close(out_fd)
        os.close(err_fd)
        out_fd = err_fd = -1

        deadline = time.time() + timeout
        rc = None
        while True:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                rc = os.waitstatus_to_exitcode(status)
                break
            if time.time() > deadline:
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except Exception:
                    pass
                raise RuntimeError(f"子进程超时（>{timeout}s）: {exe}")
            time.sleep(0.02)

        with open(out_path, encoding="utf-8", errors="ignore") as f:
            out = f.read()
        with open(err_path, encoding="utf-8", errors="ignore") as f:
            err = f.read()
        return rc, out, err
    finally:
        for fd in (out_fd, err_fd):
            if fd and fd > 0:
                try:
                    os.close(fd)
                except Exception:
                    pass
        for p in (out_path, err_path):
            try:
                os.remove(p)
            except Exception:
                pass
