# -*- coding: utf-8 -*-
"""
ProjectManager 单元测试。

回归点：
  - 新项目初始化：创建所有子目录和 project.json
  - 已有项目加载：正确读取现有 project.json
  - 路径解析：get_path 返回正确的绝对路径
  - 阶段状态更新：update_stage 正确写入并持久化
  - 单镜状态更新：update_shot 正确写入并持久化
  - 断点续跑：get_completed_shots / get_pending_shots 正确计算
  - 原子写入：project.json 保存时使用临时文件+重命名
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared.project_manager import ProjectManager, STAGE_DIRS, STAGES


@pytest.fixture
def temp_project_dir():
    """创建临时项目目录，测试后清理。"""
    tmpdir = tempfile.mkdtemp(prefix="test_project_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestProjectInit:
    """项目初始化测试。"""

    def test_new_project_creates_dirs(self, temp_project_dir):
        """新项目应创建所有标准子目录。"""
        pm = ProjectManager(temp_project_dir)
        for dirname in STAGE_DIRS.values():
            assert os.path.isdir(os.path.join(temp_project_dir, dirname))

    def test_new_project_creates_json(self, temp_project_dir):
        """新项目应创建 project.json。"""
        pm = ProjectManager(temp_project_dir)
        assert os.path.exists(pm.project_file)

    def test_new_project_default_title(self, temp_project_dir):
        """新项目标题默认为目录名。"""
        pm = ProjectManager(temp_project_dir)
        assert pm.data["title"] == os.path.basename(temp_project_dir)

    def test_new_project_version(self, temp_project_dir):
        """新项目应有版本号。"""
        pm = ProjectManager(temp_project_dir)
        assert pm.data["version"] == "1.0"

    def test_new_project_all_stages_pending(self, temp_project_dir):
        """新项目所有阶段应为 pending。"""
        pm = ProjectManager(temp_project_dir)
        for stage in STAGES:
            assert pm.get_stage_status(stage) == "pending"


class TestProjectLoad:
    """已有项目加载测试。"""

    def test_load_existing_project(self, temp_project_dir):
        """加载已有项目应保留数据。"""
        pm1 = ProjectManager(temp_project_dir)
        pm1.set_meta("custom_key", "custom_value")
        pm1.update_stage("split", "completed", output="storyboard/test.json")

        pm2 = ProjectManager(temp_project_dir)
        assert pm2.get_meta("custom_key") == "custom_value"
        assert pm2.get_stage_status("split") == "completed"
        assert pm2.get_stage_output("split") == "storyboard/test.json"

    def test_load_ensures_structure(self, temp_project_dir):
        """加载项目时自动补充缺失字段。"""
        # 手动创建一个不完整的 project.json
        incomplete = {"title": "test", "stages": {"split": {"status": "completed"}}}
        with open(os.path.join(temp_project_dir, "project.json"), "w") as f:
            json.dump(incomplete, f)

        pm = ProjectManager(temp_project_dir)
        assert "paths" in pm.data
        assert "shots" in pm.data
        assert pm.get_stage_status("split") == "completed"
        assert pm.get_stage_status("videos") == "pending"  # 补充的字段


class TestPathResolution:
    """路径解析测试。"""

    def test_get_path_returns_absolute(self, temp_project_dir):
        """get_path 应返回绝对路径。"""
        pm = ProjectManager(temp_project_dir)
        path = pm.get_path("storyboard")
        assert os.path.isabs(path)
        assert path.endswith("storyboard")

    def test_get_path_invalid_stage(self, temp_project_dir):
        """未知阶段应抛出异常。"""
        pm = ProjectManager(temp_project_dir)
        with pytest.raises(ValueError):
            pm.get_path("invalid_stage")


class TestStageUpdate:
    """阶段状态更新测试。"""

    def test_update_stage_basic(self, temp_project_dir):
        """更新阶段状态和输出。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_stage("split", "in_progress")
        assert pm.get_stage_status("split") == "in_progress"

        pm.update_stage("split", "completed", output="storyboard/xxx.json")
        assert pm.get_stage_status("split") == "completed"
        assert pm.get_stage_output("split") == "storyboard/xxx.json"

    def test_update_stage_with_extra(self, temp_project_dir):
        """更新阶段时附加额外数据。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_stage("videos", "in_progress", extra={"completed_shots": [1, 2]})
        stage_data = pm.data["stages"]["videos"]
        assert stage_data["completed_shots"] == [1, 2]

    def test_update_stage_invalid(self, temp_project_dir):
        """未知阶段应抛出异常。"""
        pm = ProjectManager(temp_project_dir)
        with pytest.raises(ValueError):
            pm.update_stage("invalid", "completed")


class TestShotUpdate:
    """单镜状态更新测试。"""

    def test_update_shot_basic(self, temp_project_dir):
        """更新单镜状态。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_shot(1, "completed", video="videos/shot_01.mp4")
        assert pm.get_shot_status(1) == "completed"

    def test_update_shot_with_error(self, temp_project_dir):
        """更新失败的镜头。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_shot(2, "failed", error="额度耗尽")
        assert pm.get_shot_status(2) == "failed"
        shot_data = pm.data["shots"]["2"]
        assert shot_data["error"] == "额度耗尽"

    def test_get_completed_shots(self, temp_project_dir):
        """获取已完成镜号列表。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_shot(1, "completed")
        pm.update_shot(2, "failed")
        pm.update_shot(3, "completed")
        assert pm.get_completed_shots() == [1, 3]

    def test_get_pending_shots(self, temp_project_dir):
        """获取待生成镜号列表。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_shot(1, "completed")
        pm.update_shot(3, "completed")
        assert pm.get_pending_shots(5) == [2, 4, 5]


class TestAtomicSave:
    """原子写入测试。"""

    def test_save_creates_valid_json(self, temp_project_dir):
        """保存后的 project.json 应为有效 JSON。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_stage("split", "completed")

        with open(pm.project_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["stages"]["split"]["status"] == "completed"

    def test_no_temp_files_left(self, temp_project_dir):
        """保存后不应留下临时文件。"""
        pm = ProjectManager(temp_project_dir)
        pm.update_stage("split", "completed")
        pm.update_shot(1, "completed")

        files = os.listdir(temp_project_dir)
        temp_files = [f for f in files if f.startswith(".project_")]
        assert len(temp_files) == 0


class TestResolveOutputDir:
    """输出目录解析测试。"""

    def test_resolve_with_project_root(self, temp_project_dir):
        """使用 project_root 时返回 ProjectManager。"""
        from shared.project_manager import resolve_output_dir
        out_dir, pm = resolve_output_dir(temp_project_dir, None)
        assert out_dir == os.path.abspath(temp_project_dir)
        assert pm is not None
        assert isinstance(pm, ProjectManager)

    def test_resolve_with_out_only(self, temp_project_dir):
        """仅使用 -o 时返回 None ProjectManager。"""
        from shared.project_manager import resolve_output_dir
        out_dir, pm = resolve_output_dir(None, temp_project_dir)
        assert out_dir == os.path.abspath(temp_project_dir)
        assert pm is None

    def test_resolve_default_cwd(self):
        """都未指定时使用当前目录。"""
        from shared.project_manager import resolve_output_dir
        out_dir, pm = resolve_output_dir(None, None)
        assert out_dir == os.getcwd()
        assert pm is None
