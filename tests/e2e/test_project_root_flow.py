# -*- coding: utf-8 -*-
"""
项目根目录模式端到端测试。

测试目标：
  验证 --project-root 模式下，产物正确归档到子目录，
  project.json 正确记录各阶段状态。

运行方式：
  # 完整流程（需要额度）
  python -m pytest tests/e2e/test_project_root_flow.py -v

  # 仅验证目录结构（不消耗额度）
  python -m pytest tests/e2e/test_project_root_flow.py::test_project_structure -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared.project_manager import ProjectManager


@pytest.fixture
def temp_project_root():
    """创建临时项目目录，测试后清理。"""
    tmpdir = tempfile.mkdtemp(prefix="test_e2e_project_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def sample_novel_file(tmp_path):
    """创建测试用小说文件。"""
    novel_path = tmp_path / "test_novel.txt"
    novel_path.write_text("""雨夜，客栈。

苏见川收剑入鞘，转身离去。

白若霜望着地上的人，眼眶泛红："终于结束了。"
""", encoding="utf-8")
    return str(novel_path)


class TestProjectStructure:
    """项目目录结构测试（不消耗额度）。"""

    def test_project_structure(self, temp_project_root):
        """验证项目根目录创建所有标准子目录。"""
        pm = ProjectManager(temp_project_root)

        # 验证所有子目录存在
        for dirname in ["storyboard", "characters", "keyframes", "videos", "output", "logs"]:
            assert os.path.isdir(os.path.join(temp_project_root, dirname)), \
                f"子目录 {dirname} 不存在"

        # 验证 project.json 存在
        assert os.path.exists(os.path.join(temp_project_root, "project.json"))

        # 验证 project.json 结构
        with open(os.path.join(temp_project_root, "project.json"), encoding="utf-8") as f:
            data = json.load(f)
        assert "version" in data
        assert "stages" in data
        assert "paths" in data
        assert data["stages"]["split"]["status"] == "pending"
        assert data["stages"]["videos"]["status"] == "pending"

    def test_split_novel_with_project_root(self, temp_project_root, sample_novel_file):
        """验证 split_novel.py --project-root 模式。"""
        scripts_dir = os.path.join(
            _ROOT, "skills", "doubao-manju-video", "scripts"
        )
        split_novel = os.path.join(scripts_dir, "split_novel.py")

        result = subprocess.run(
            [sys.executable, split_novel, sample_novel_file,
             "-p", temp_project_root, "--json-only"],
            capture_output=True, text=True, timeout=60
        )

        assert result.returncode == 0, f"split_novel 失败: {result.stderr}"

        # 验证产物在 storyboard/ 子目录
        storyboard_dir = os.path.join(temp_project_root, "storyboard")
        shots_files = [f for f in os.listdir(storyboard_dir) if f.endswith(".shots.json")]
        assert len(shots_files) > 0, "storyboard/ 下无 shots.json"

        # 验证 project.json 更新
        with open(os.path.join(temp_project_root, "project.json"), encoding="utf-8") as f:
            data = json.load(f)
        assert data["stages"]["split"]["status"] == "completed"
        assert "output" in data["stages"]["split"]
        assert data["stages"]["split"]["output"].startswith("storyboard/")


@pytest.mark.skip(reason="需要豆包额度，手动运行")
class TestFullProjectFlow:
    """完整项目流程测试（需要额度）。"""

    def test_full_flow(self, temp_project_root, sample_novel_file):
        """完整流程：split → char_ref → doubao_gen。"""
        scripts_dir = os.path.join(
            _ROOT, "skills", "doubao-manju-video", "scripts"
        )
        char_ref_dir = os.path.join(
            _ROOT, "skills", "doubao-character-ref", "scripts"
        )

        # Step 1: 分镜拆解
        result = subprocess.run(
            [sys.executable, os.path.join(scripts_dir, "split_novel.py"),
             sample_novel_file, "-p", temp_project_root],
            capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, f"split_novel 失败: {result.stderr}"

        # Step 2: 角色参考图（需要额度，可能失败）
        # result = subprocess.run(
        #     [sys.executable, os.path.join(char_ref_dir, "char_ref_gen.py"),
        #      "-p", temp_project_root, "--dry-run"],
        #     capture_output=True, text=True, timeout=60
        # )

        # Step 3: 视频生成（需要额度，可能失败）
        # result = subprocess.run(
        #     [sys.executable, os.path.join(scripts_dir, "doubao_gen.py"),
        #      "-p", temp_project_root, "--dry-run"],
        #     capture_output=True, text=True, timeout=60
        # )

        # 验证 project.json 状态
        with open(os.path.join(temp_project_root, "project.json"), encoding="utf-8") as f:
            data = json.load(f)
        assert data["stages"]["split"]["status"] == "completed"
