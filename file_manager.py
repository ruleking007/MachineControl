"""录制文件管理器（可单独运行，也作为 main.py 的一个页签）。

改造要点：C1 只列文件；C2/C3/C4 全链路异常处理；C5 双击导出、删除后保持选中。
"""
from __future__ import annotations

import os
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import common


class FileManagerFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.pack(fill=tk.BOTH, expand=True)

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="🔄 刷新列表", command=self.refresh).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="🗑 删除选中", command=self.delete).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="📤 导出选中", command=self.export).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="📁 打开文件夹", command=self.open_folder).pack(side=tk.LEFT, padx=2)

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, pady=8)
        scroll = ttk.Scrollbar(body)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox = tk.Listbox(body, yscrollcommand=scroll.set,
                                  font=("Consolas", 10), activestyle="dotbox")
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=self.listbox.yview)
        self.listbox.bind("<Double-1>", lambda e: self.export())

        self.info = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.info, foreground="#0b5394").pack(fill=tk.X)

        self.refresh()

    # ---------- 数据 ----------
    def refresh(self) -> None:
        keep = self.selected()
        self.listbox.delete(0, tk.END)
        try:
            files = common.list_recordings()
        except OSError as exc:
            self.info.set(f"读取目录失败：{exc}")
            return
        for name in files:
            self.listbox.insert(tk.END, name)
        if files:
            index = files.index(keep) if keep in files else 0
            self.listbox.selection_set(index)
            self.listbox.see(index)
        self.info.set(f"共 {len(files)} 个录制文件 @ {common.RECORDS_DIR}")

    def selected(self) -> str:
        sel = self.listbox.curselection()
        return self.listbox.get(sel[0]) if sel else ""

    def _path(self, name: str) -> str:
        return os.path.join(common.RECORDS_DIR, name)

    # ---------- 操作 ----------
    def delete(self) -> None:
        name = self.selected()
        if not name:
            messagebox.showwarning("提示", "请先选择一个文件")
            return
        if not messagebox.askyesno("确认", f"删除 {name}？不可恢复。"):
            return
        try:
            os.remove(self._path(name))
        except OSError as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.refresh()

    def export(self) -> None:
        name = self.selected()
        if not name:
            messagebox.showwarning("提示", "请先选择一个文件")
            return
        ext = os.path.splitext(name)[1]
        dst = filedialog.asksaveasfilename(initialfile=name, defaultextension=ext,
                                           filetypes=[("录制文件", f"*{ext}"), ("所有文件", "*.*")])
        if not dst:
            return
        try:
            shutil.copy2(self._path(name), dst)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self.info.set(f"✅ 已导出到 {dst}")

    def open_folder(self) -> None:
        try:
            common.ensure_dirs()
            os.startfile(common.RECORDS_DIR)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def shutdown(self) -> None:
        return


# 兼容旧名字/独立运行
def FileManager(root: tk.Tk) -> FileManagerFrame:
    root.title("录制文件管理")
    root.geometry("620x480")
    return FileManagerFrame(root)


if __name__ == "__main__":
    root = tk.Tk()
    FileManager(root)
    root.mainloop()
