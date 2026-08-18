"""PromptRegistry 单元测试。"""
import tempfile
from pathlib import Path

import pytest
import yaml

from app.services.prompt_registry import PromptRegistry, PromptTemplate, get_prompt


@pytest.fixture
def temp_prompts_dir():
    """创建临时 prompts 目录用于测试。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir) / "prompts"
        prompts_dir.mkdir()
        yield prompts_dir


@pytest.fixture
def registry_with_temp_dir(temp_prompts_dir, monkeypatch):
    """使用临时目录的 PromptRegistry 实例。"""
    # 重置单例
    PromptRegistry.reset()
    
    # 创建测试 YAML 文件
    test_prompts = {
        "test_prompt.yaml": {
            "name": "test_prompt",
            "version": "v1",
            "system": "这是一个测试 prompt\n包含多行内容",
            "template": None
        },
        "template_prompt.yaml": {
            "name": "template_prompt",
            "version": "v2",
            "system": "默认系统消息",
            "template": "你好 {name}，欢迎来到 {place}"
        },
        "invalid.yaml": "这不是一个字典"
    }
    
    for filename, content in test_prompts.items():
        with open(temp_prompts_dir / filename, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
    
    # 猴子补丁替换 _PROMPTS_DIR
    import app.services.prompt_registry as module
    monkeypatch.setattr(module, "_PROMPTS_DIR", temp_prompts_dir)
    
    # 获取新实例
    registry = PromptRegistry.get_instance()
    yield registry
    
    # 清理
    PromptRegistry.reset()


def test_load_yaml_files(registry_with_temp_dir):
    """测试从 YAML 文件加载 prompt。"""
    registry = registry_with_temp_dir
    
    # 应该加载了 2 个有效文件（invalid.yaml 会被跳过）
    prompts = registry.list_prompts()
    assert len(prompts) == 2
    
    names = {p["name"] for p in prompts}
    assert "test_prompt" in names
    assert "template_prompt" in names


def test_get_existing_prompt(registry_with_temp_dir):
    """测试获取存在的 prompt。"""
    registry = registry_with_temp_dir
    
    prompt = registry.get("test_prompt")
    assert prompt.name == "test_prompt"
    assert prompt.version == "v1"
    assert "这是一个测试 prompt" in prompt.system
    assert "包含多行内容" in prompt.system
    assert prompt.template is None


def test_get_nonexistent_prompt_returns_empty(registry_with_temp_dir):
    """测试获取不存在的 prompt 返回空模板。"""
    registry = registry_with_temp_dir
    
    prompt = registry.get("nonexistent")
    assert prompt.name == "nonexistent"
    assert prompt.version == "v0"
    assert prompt.system == ""
    assert prompt.template is None


def test_render_without_template(registry_with_temp_dir):
    """测试没有 template 字段的 render() 返回 system。"""
    registry = registry_with_temp_dir
    
    prompt = registry.get("test_prompt")
    rendered = prompt.render()
    assert rendered == prompt.system


def test_render_with_template(registry_with_temp_dir):
    """测试有 template 字段的 render() 使用 kwargs 渲染。"""
    registry = registry_with_temp_dir
    
    prompt = registry.get("template_prompt")
    assert prompt.template is not None
    
    rendered = prompt.render(name="Alice", place="Wonderland")
    assert rendered == "你好 Alice，欢迎来到 Wonderland"


def test_render_with_missing_key_falls_back_to_system(registry_with_temp_dir):
    """测试 template 渲染缺少 key 时回退到 system。"""
    registry = registry_with_temp_dir
    
    prompt = registry.get("template_prompt")
    
    # 缺少 place 参数
    rendered = prompt.render(name="Alice")
    assert rendered == prompt.system  # 回退到 system


def test_reload_clears_and_reloads(registry_with_temp_dir, temp_prompts_dir):
    """测试 reload() 清空并重新加载。"""
    registry = registry_with_temp_dir
    
    # 初始加载
    initial_prompts = registry.list_prompts()
    assert len(initial_prompts) == 2
    
    # 添加新文件
    new_prompt = {
        "name": "new_prompt",
        "version": "v3",
        "system": "新的 prompt 内容"
    }
    with open(temp_prompts_dir / "new_prompt.yaml", "w", encoding="utf-8") as f:
        yaml.dump(new_prompt, f, allow_unicode=True)
    
    # reload 后应该包含新文件
    registry.reload()
    updated_prompts = registry.list_prompts()
    assert len(updated_prompts) == 3
    
    names = {p["name"] for p in updated_prompts}
    assert "new_prompt" in names


def test_get_prompt_convenience_function(registry_with_temp_dir):
    """测试 get_prompt() 快捷函数。"""
    prompt = get_prompt("test_prompt")
    assert prompt.name == "test_prompt"
    assert prompt.version == "v1"


def test_singleton_pattern():
    """测试单例模式。"""
    PromptRegistry.reset()
    
    registry1 = PromptRegistry.get_instance()
    registry2 = PromptRegistry.get_instance()
    
    assert registry1 is registry2
    
    PromptRegistry.reset()


def test_version_field(registry_with_temp_dir):
    """测试 version 字段正确加载。"""
    registry = registry_with_temp_dir
    
    prompt1 = registry.get("test_prompt")
    assert prompt1.version == "v1"
    
    prompt2 = registry.get("template_prompt")
    assert prompt2.version == "v2"


def test_system_content_preserves_newlines(registry_with_temp_dir):
    """测试 system 内容保留换行符。"""
    registry = registry_with_temp_dir
    
    prompt = registry.get("test_prompt")
    assert "\n" in prompt.system
    lines = prompt.system.split("\n")
    assert len(lines) >= 2
