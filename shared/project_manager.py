# -*- coding: utf-8 -*-
"""
项目管理器 —— 统一的项目根目录与产物管理。

用途：
  为漫剧生成流水线提供统一的项目锚点，管理 project.json 元数据，
  自动创建子目录结构，支持断点续跑时状态恢复。

核心概念：
  project_root: 项目根目录，所有产物以此为基础组织
  project.json: 项目元数据文件，记录各阶段状态和产物路径

目录结构：
  <project-root>/
    project.json          # 项目元数据
    storyboard/           # 分镜阶段产物
    characters/           # 角色/道具参考图
    keyframes/            # 关键帧图
    videos/               # 原始视频
    output/               # 预留：最终成片（流水线暂不自动写入）
    logs/                 # 预留：运行日志

使用方式：
  from shared.project_manager import ProjectManager

  # 初始化或加载项目
  pm = ProjectManager("path/to/project")

  # 获取子目录路径
  storyboard_dir = pm.get_path("storyboard")

  # 更新阶段状态
  pm.update_stage("split", "completed", output="storyboard/xxx.shots.json")

  # 更新单镜状态
  pm.update_shot(1, "completed", video="videos/shot_01_14s.mp4")
"""

import json
import os
import tempfile
from datetime import datetime
from typing import Any, Optional


# 标准子目录名称
STAGE_DIRS = {
    "storyboard": "storyboard",
    "characters": "characters",
    "keyframes": "keyframes",
    "videos": "videos",
    "output": "output",
    "logs": "logs",
}

# 阶段名称
STAGES = ["split", "characters", "keyframes", "videos", "concat"]


class ProjectManager:
    """项目管理器：负责 project.json 读写、子目录创建、路径解析。"""

    def __init__(self, project_root: str):
        """
        初始化项目管理器。

        project_root: 项目根目录路径，不存在时会创建
        """
        self.root = os.path.abspath(project_root)
        self.project_file = os.path.join(self.root, "project.json")
        self._data: dict[str, Any] = {}
        self._load_or_init()

    def _load_or_init(self) -> None:
        """加载已有 project.json 或初始化新项目。"""
        if os.path.exists(self.project_file):
            with open(self.project_file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            # 确保所有标准字段存在
            self._ensure_structure()
        else:
            self._init_new()

    def _init_new(self) -> None:
        """初始化新项目结构。"""
        now = datetime.now().isoformat()
        self._data = {
            "version": "1.0",
            "title": os.path.basename(self.root),
            "created_at": now,
            "updated_at": now,
            "paths": {k: f"{v}/" for k, v in STAGE_DIRS.items()},
            "stages": {s: {"status": "pending"} for s in STAGES},
            "shots": {},
        }
        self._create_dirs()
        self._save()

    def _ensure_structure(self) -> None:
        """确保数据结构完整，补充缺失字段。"""
        now = datetime.now().isoformat()
        self._data.setdefault("version", "1.0")
        self._data.setdefault("title", os.path.basename(self.root))
        self._data.setdefault("created_at", now)
        self._data.setdefault("updated_at", now)
        self._data.setdefault("paths", {k: f"{v}/" for k, v in STAGE_DIRS.items()})
        self._data.setdefault("stages", {s: {"status": "pending"} for s in STAGES})
        self._data.setdefault("shots", {})
        # 补充缺失的阶段
        for s in STAGES:
            if s not in self._data["stages"]:
                self._data["stages"][s] = {"status": "pending"}

    def _create_dirs(self) -> None:
        """创建所有标准子目录。"""
        os.makedirs(self.root, exist_ok=True)
        for dirname in STAGE_DIRS.values():
            os.makedirs(os.path.join(self.root, dirname), exist_ok=True)

    def _save(self) -> None:
        """保存 project.json（原子写入，防止损坏）。"""
        self._data["updated_at"] = datetime.now().isoformat()
        # 使用临时文件 + 原子重命名
        fd, tmp_path = tempfile.mkstemp(
            dir=self.root, prefix=".project_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            # Windows 下需要先删除目标文件才能重命名
            if os.path.exists(self.project_file):
                os.remove(self.project_file)
            os.rename(tmp_path, self.project_file)
        except Exception:
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    @property
    def data(self) -> dict[str, Any]:
        """获取项目数据（只读）。"""
        return self._data.copy()

    def get_path(self, stage: str) -> str:
        """
        获取指定阶段的子目录绝对路径。

        stage: storyboard/characters/keyframes/videos/output/logs
        """
        if stage not in STAGE_DIRS:
            raise ValueError(f"未知的阶段目录: {stage}")
        return os.path.join(self.root, STAGE_DIRS[stage])

    def get_stage_output(self, stage: str) -> Optional[str]:
        """获取指定阶段的输出文件路径（相对项目根目录）。"""
        stage_data = self._data.get("stages", {}).get(stage, {})
        return stage_data.get("output")

    def get_stage_status(self, stage: str) -> str:
        """获取指定阶段的状态：pending/in_progress/completed/failed"""
        stage_data = self._data.get("stages", {}).get(stage, {})
        return stage_data.get("status", "pending")

    def update_stage(
        self,
        stage: str,
        status: str,
        output: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        更新阶段状态。

        stage: 阶段名称（split/characters/keyframes/videos/concat）
        status: 状态（pending/in_progress/completed/failed）
        output: 该阶段的主要输出文件路径（相对项目根目录）
        extra: 额外的阶段数据
        """
        if stage not in STAGES:
            raise ValueError(f"未知的阶段: {stage}")
        stage_data = {"status": status}
        if output:
            stage_data["output"] = output.replace("\\", "/")
        if extra:
            stage_data.update(extra)
        self._data["stages"][stage] = stage_data
        self._save()

    def update_shot(
        self,
        shot_idx: int,
        status: str,
        video: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        更新单镜状态。

        shot_idx: 镜头编号（从 1 开始）
        status: 状态（pending/in_progress/completed/failed）
        video: 视频文件路径（相对项目根目录）
        error: 错误信息
        """
        key = str(shot_idx)
        shot_data = {"status": status}
        if video:
            shot_data["video"] = video.replace("\\", "/")
        if error:
            shot_data["error"] = error
        self._data["shots"][key] = shot_data
        self._save()

    def get_shot_status(self, shot_idx: int) -> str:
        """获取单镜状态。"""
        key = str(shot_idx)
        return self._data.get("shots", {}).get(key, {}).get("status", "pending")

    def get_completed_shots(self) -> list[int]:
        """获取已完成的镜号列表。"""
        completed = []
        for key, data in self._data.get("shots", {}).items():
            if data.get("status") == "completed":
                try:
                    completed.append(int(key))
                except ValueError:
                    pass
        return sorted(completed)

    def get_pending_shots(self, total_shots: int) -> list[int]:
        """获取待生成的镜号列表。"""
        completed = set(self.get_completed_shots())
        return [i for i in range(1, total_shots + 1) if i not in completed]

    def set_meta(self, key: str, value: Any) -> None:
        """设置项目级元数据（如 title/style_key/ratio）。"""
        self._data[key] = value
        self._save()

    def get_meta(self, key: str, default: Any = None) -> Any:
        """获取项目级元数据。"""
        return self._data.get(key, default)


def resolve_output_dir(project_root: Optional[str], out_arg: Optional[str]) -> tuple[str, Optional[ProjectManager]]:
    """
    解析输出目录。

    优先使用 project_root，其次使用 out_arg，最后使用当前目录。

    返回: (输出目录路径, ProjectManager 实例或 None)
    """
    if project_root:
        pm = ProjectManager(project_root)
        return pm.root, pm
    elif out_arg:
        return os.path.abspath(out_arg), None
    else:
        return os.getcwd(), None


def get_default_workspace() -> str:
    """
    获取默认工作区路径。

    位于 skill 目录下的 workspace/ 子目录。
    """
    # 从 shared/ 向上两级到项目根目录
    skill_root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(skill_root, "workspace")
