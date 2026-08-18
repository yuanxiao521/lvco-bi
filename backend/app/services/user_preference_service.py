"""用户偏好记忆服务

实现用户偏好的 CRUD 操作，支持：
- 显式偏好：用户明确表达（如"我喜欢折线图"）
- 隐式偏好：从用户行为推断（如连续3次选择折线图）
- 偏好强度衰减：长时间未使用的偏好自动衰减
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from app.models.user_preference import UserPreference
from app.repositories.protocols import UserPreferenceRepository

logger = logging.getLogger(__name__)


class UserPreferenceService:
    """用户偏好记忆服务"""
    
    # 偏好类型常量
    CHART_TYPE = "chart_type"
    COLOR_SCHEME = "color_scheme"
    DIMENSION = "dimension"
    AGGREGATION = "aggregation"
    ANALYSIS_FOCUS = "analysis_focus"
    
    # 偏好强度衰减配置
    DECAY_DAYS = 30  # 30天未使用开始衰减
    DECAY_RATE = 0.95  # 每次衰减系数
    MIN_STRENGTH = 0.1  # 最低强度阈值
    
    def __init__(self, user_preference_repo: UserPreferenceRepository):
        self.user_preference_repo = user_preference_repo
    
    async def get_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str
    ) -> Optional[UserPreference]:
        """获取单个偏好
        
        Args:
            user_id: 用户ID
            preference_type: 偏好类型
            preference_key: 偏好键
            
        Returns:
            UserPreference 或 None
        """
        logger.info(
            f"get_preference: user_id={user_id}, type={preference_type}, key={preference_key}"
        )
        
        preference = await self.user_preference_repo.get_preference(
            user_id, preference_type, preference_key
        )
        
        if preference:
            logger.info(f"get_preference: found, strength={preference.strength:.2f}")
        else:
            logger.info("get_preference: not found")
        
        return preference
    
    async def get_user_preferences(
        self,
        user_id: UUID,
        preference_type: Optional[str] = None
    ) -> list[UserPreference]:
        """获取用户的所有偏好（可选按类型过滤）
        
        Args:
            user_id: 用户ID
            preference_type: 偏好类型（可选）
            
        Returns:
            偏好列表，按强度降序排列
        """
        logger.info(f"get_user_preferences: user_id={user_id}, type={preference_type}")
        
        preferences = await self.user_preference_repo.get_user_preferences(
            user_id, preference_type
        )
        
        logger.info(f"get_user_preferences: found {len(preferences)} preferences")
        
        return preferences
    
    async def set_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
        preference_value: dict,
        strength: float = 0.5,
        is_explicit: bool = False
    ) -> UserPreference:
        """设置偏好（创建或更新）

        Args:
            user_id: 用户ID
            preference_type: 偏好类型
            preference_key: 偏好键
            preference_value: 偏好值（JSON）
            strength: 初始强度（0-1）
            is_explicit: 是否显式表达（显式=1.0，隐式=0.3-0.7）

        Returns:
            创建或更新的 UserPreference
        """
        logger.info(
            f"set_preference: user_id={user_id}, type={preference_type}, "
            f"key={preference_key}, explicit={is_explicit}"
        )

        # 查找现有偏好
        existing = await self.get_preference(user_id, preference_type, preference_key)

        if existing:
            # 更新现有偏好
            existing.preference_value = preference_value
            existing.evidence_count += 1
            existing.last_used_at = datetime.utcnow()

            # 根据表达方式调整强度
            if is_explicit:
                existing.strength = 1.0
            else:
                # 隐式偏好：强度随证据数量增长，但不超过1.0
                existing.strength = min(1.0, existing.strength + 0.1)

            await self.user_preference_repo.update_preference(
                existing, preference_value, existing.strength, existing.evidence_count
            )
            logger.info(
                f"set_preference: updated, strength={existing.strength:.2f}, "
                f"evidence={existing.evidence_count}"
            )
            return existing
        else:
            # 创建新偏好
            preference = await self.user_preference_repo.create_preference(
                user_id=user_id,
                preference_type=preference_type,
                preference_key=preference_key,
                preference_value=preference_value,
                strength=1.0 if is_explicit else strength,
                evidence_count=1,
            )

            logger.info(
                f"set_preference: created, strength={preference.strength:.2f}"
            )
            return preference
    
    async def record_implicit_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
        preference_value: dict
    ) -> UserPreference:
        """记录隐式偏好（从用户行为推断）
        
        Args:
            user_id: 用户ID
            preference_type: 偏好类型
            preference_key: 偏好键
            preference_value: 偏好值
            
        Returns:
            创建或更新的 UserPreference
        """
        logger.info(
            f"record_implicit_preference: user_id={user_id}, "
            f"type={preference_type}, key={preference_key}"
        )
        
        return await self.set_preference(
            user_id=user_id,
            preference_type=preference_type,
            preference_key=preference_key,
            preference_value=preference_value,
            strength=0.5,  # 隐式偏好初始强度
            is_explicit=False
        )
    
    async def record_explicit_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
        preference_value: dict
    ) -> UserPreference:
        """记录显式偏好（用户明确表达）
        
        Args:
            user_id: 用户ID
            preference_type: 偏好类型
            preference_key: 偏好键
            preference_value: 偏好值
            
        Returns:
            创建或更新的 UserPreference
        """
        logger.info(
            f"record_explicit_preference: user_id={user_id}, "
            f"type={preference_type}, key={preference_key}"
        )
        
        return await self.set_preference(
            user_id=user_id,
            preference_type=preference_type,
            preference_key=preference_key,
            preference_value=preference_value,
            strength=1.0,
            is_explicit=True
        )
    
    async def delete_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str
    ) -> bool:
        """删除偏好
        
        Args:
            user_id: 用户ID
            preference_type: 偏好类型
            preference_key: 偏好键
            
        Returns:
            是否删除成功
        """
        logger.info(
            f"delete_preference: user_id={user_id}, "
            f"type={preference_type}, key={preference_key}"
        )
        
        preference = await self.get_preference(user_id, preference_type, preference_key)
        
        if preference:
            await self.user_preference_repo.delete_preference(preference)
            logger.info("delete_preference: deleted")
            return True
        else:
            logger.info("delete_preference: not found")
            return False
    
    async def apply_decay(self, user_id: UUID) -> int:
        """应用偏好强度衰减
        
        对于超过 DECAY_DAYS 未使用的偏好，自动降低强度。
        强度低于 MIN_STRENGTH 的偏好会被删除。
        
        Args:
            user_id: 用户ID
            
        Returns:
            衰减的偏好数量
        """
        logger.info(f"apply_decay: user_id={user_id}")
        
        # 获取所有偏好
        preferences = await self.get_user_preferences(user_id)
        
        decay_count = 0
        delete_count = 0
        now = datetime.utcnow()
        
        for pref in preferences:
            if pref.last_used_at:
                last_used = pref.last_used_at.replace(tzinfo=None)
                days_since_use = (now - last_used).days
                
                if days_since_use > self.DECAY_DAYS:
                    # 计算衰减次数（每30天衰减一次）
                    decay_times = days_since_use // self.DECAY_DAYS
                    new_strength = pref.strength * (self.DECAY_RATE ** decay_times)
                    
                    if new_strength < self.MIN_STRENGTH:
                        # 强度过低，删除偏好
                        await self.user_preference_repo.delete_preference(pref)
                        delete_count += 1
                        logger.info(
                            f"apply_decay: deleted preference "
                            f"{pref.preference_type}:{pref.preference_key} "
                            f"(strength={new_strength:.2f} < {self.MIN_STRENGTH})"
                        )
                    else:
                        # 更新强度
                        await self.user_preference_repo.update_preference(
                            pref, pref.preference_value, new_strength, pref.evidence_count
                        )
                        decay_count += 1
                        logger.info(
                            f"apply_decay: decayed preference "
                            f"{pref.preference_type}:{pref.preference_key} "
                            f"from {pref.strength:.2f} to {new_strength:.2f}"
                        )
        
        logger.info(
            f"apply_decay: completed, decayed={decay_count}, deleted={delete_count}"
        )
        
        return decay_count
    
    async def get_top_preferences(
        self,
        user_id: UUID,
        preference_type: str,
        limit: int = 3
    ) -> list[UserPreference]:
        """获取某类型的前N个最强偏好
        
        Args:
            user_id: 用户ID
            preference_type: 偏好类型
            limit: 返回数量
            
        Returns:
            偏好列表，按强度降序排列
        """
        logger.info(
            f"get_top_preferences: user_id={user_id}, "
            f"type={preference_type}, limit={limit}"
        )
        
        preferences = await self.user_preference_repo.get_top_preferences(
            user_id, preference_type, limit
        )
        
        logger.info(f"get_top_preferences: found {len(preferences)} preferences")
        
        return preferences
    
    async def format_preferences_for_prompt(
        self,
        user_id: UUID,
        preference_types: Optional[list[str]] = None
    ) -> str:
        """格式化用户偏好，用于注入到 prompt
        
        Args:
            user_id: 用户ID
            preference_types: 偏好类型列表（可选，默认全部）
            
        Returns:
            格式化的偏好描述字符串
        """
        logger.info(f"format_preferences_for_prompt: user_id={user_id}")
        
        if preference_types:
            # 获取指定类型的偏好
            all_preferences = []
            for pref_type in preference_types:
                preferences = await self.get_top_preferences(user_id, pref_type, limit=3)
                all_preferences.extend(preferences)
        else:
            # 获取所有偏好
            all_preferences = await self.get_user_preferences(user_id)
        
        if not all_preferences:
            logger.info("format_preferences_for_prompt: no preferences found")
            return ""
        
        # 按类型分组
        preferences_by_type = {}
        for pref in all_preferences:
            if pref.preference_type not in preferences_by_type:
                preferences_by_type[pref.preference_type] = []
            preferences_by_type[pref.preference_type].append(pref)
        
        # 格式化输出
        lines = ["用户偏好："]
        for pref_type, prefs in preferences_by_type.items():
            type_name = self._get_type_display_name(pref_type)
            pref_items = []
            for pref in prefs:
                strength_pct = int(pref.strength * 100)
                pref_items.append(f"{pref.preference_key}({strength_pct}%)")
            lines.append(f"- {type_name}: {', '.join(pref_items)}")
        
        result = "\n".join(lines)
        logger.info(f"format_preferences_for_prompt: formatted {len(lines)-1} types")
        
        return result
    
    def _get_type_display_name(self, preference_type: str) -> str:
        """获取偏好类型的显示名称"""
        type_names = {
            self.CHART_TYPE: "图表类型",
            self.COLOR_SCHEME: "配色方案",
            self.DIMENSION: "分析维度",
            self.AGGREGATION: "聚合函数",
            self.ANALYSIS_FOCUS: "分析关注点"
        }
        return type_names.get(preference_type, preference_type)
