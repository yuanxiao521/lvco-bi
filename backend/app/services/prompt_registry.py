"""Prompt 模板注册表。

从 YAML 文件加载 prompt，支持版本管理和运行时切换。
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("lvco.prompt_registry")

# prompts 目录：backend/prompts/
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class PromptTemplate:
    """单个 prompt 模板。"""

    def __init__(self, name: str, version: str, system: str, template: str | None = None):
        self.name = name
        self.version = version
        self.system = system
        self.template = template

    def render(self, **kwargs: Any) -> str:
        """如果有 template 字段，用 kwargs 渲染；否则返回 system。"""
        logger.debug("prompt_render_start name=%s version=%s has_template=%s kwargs_count=%d", 
                    self.name, self.version, bool(self.template), len(kwargs))
        if self.template:
            try:
                result = self.template.format(**kwargs)
                logger.debug("prompt_render_success name=%s result_length=%d", self.name, len(result))
                return result
            except KeyError as e:
                logger.warning("prompt_render_missing_key name=%s key=%s kwargs_keys=%s", 
                             self.name, e, list(kwargs.keys()))
                return self.system
        logger.debug("prompt_render_no_template name=%s returning_system", self.name)
        return self.system


class PromptRegistry:
    """Prompt 注册表（单例）。

    用法：
        registry = PromptRegistry.get_instance()
        prompt = registry.get("chat_system")
        system_text = prompt.system
    """

    _instance: PromptRegistry | None = None

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        self._load_all()

    @classmethod
    def get_instance(cls) -> PromptRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（测试用）。"""
        cls._instance = None

    def _load_all(self) -> None:
        """从 prompts/ 目录加载所有 YAML。"""
        logger.info("prompt_load_all_start prompts_dir=%s", _PROMPTS_DIR)
        if not _PROMPTS_DIR.exists():
            logger.warning("prompts_dir_not_found path=%s", _PROMPTS_DIR)
            return
        
        yaml_files = list(_PROMPTS_DIR.glob("*.yaml"))
        logger.debug("prompt_load_all_found_files count=%d files=%s", 
                    len(yaml_files), [f.name for f in yaml_files])
        
        loaded_count = 0
        for yaml_file in yaml_files:
            try:
                logger.debug("prompt_loading_file file=%s", yaml_file.name)
                with open(yaml_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    logger.warning("prompt_invalid_format file=%s type=%s", 
                                 yaml_file.name, type(data).__name__)
                    continue
                name = data.get("name", yaml_file.stem)
                version = data.get("version", "v1")
                system = data.get("system", "")
                if isinstance(system, str):
                    system = textwrap.dedent(system).strip("\n")
                template = data.get("template")
                if isinstance(template, str):
                    template = textwrap.dedent(template).strip("\n")
                self._templates[name] = PromptTemplate(
                    name=name, version=version, system=system, template=template
                )
                loaded_count += 1
                logger.info("prompt_loaded name=%s version=%s system_length=%d has_template=%s", 
                           name, version, len(system), bool(template))
            except Exception as e:
                logger.warning("prompt_load_failed file=%s error=%s error_type=%s", 
                             yaml_file.name, e, type(e).__name__, exc_info=True)
        
        logger.info("prompt_load_all_complete loaded=%d total_files=%d", 
                   loaded_count, len(yaml_files))

    def get(self, name: str) -> PromptTemplate:
        """获取指定名称的 prompt 模板。"""
        logger.debug("prompt_get name=%s available_count=%d", name, len(self._templates))
        tmpl = self._templates.get(name)
        if tmpl is None:
            logger.warning("prompt_not_found name=%s available_names=%s", 
                         name, list(self._templates.keys()))
            # 返回空模板而不是抛异常，保证向后兼容
            return PromptTemplate(name=name, version="v0", system="")
        logger.debug("prompt_get_success name=%s version=%s", name, tmpl.version)
        return tmpl

    def list_prompts(self) -> list[dict[str, str]]:
        """列出所有已加载的 prompt。"""
        return [
            {"name": t.name, "version": t.version}
            for t in self._templates.values()
        ]

    def reload(self) -> None:
        """重新加载所有 prompt（热更新）。"""
        logger.info("prompt_registry_reload_start previous_count=%d", len(self._templates))
        self._templates.clear()
        self._load_all()
        logger.info("prompt_registry_reload_complete new_count=%d", len(self._templates))


def get_prompt(name: str) -> PromptTemplate:
    """快捷函数：获取 prompt 模板。"""
    return PromptRegistry.get_instance().get(name)
