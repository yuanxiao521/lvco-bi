/**
 * 画布 Block 对齐/吸附 Hook。
 * 
 * 功能：
 * 1. 网格吸附：拖拽时 position 按 gridSize=8 取整
 * 2. 边缘对齐：与其他 Block 边缘对齐时（误差 ≤4px）返回对齐坐标和辅助线
 * 3. Alt 键禁用吸附
 */

export interface BlockPosition {
  x: number;
  y: number;
}

export interface BlockBounds {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface AlignmentResult {
  position: BlockPosition;
  guides: AlignmentGuide[];
}

export interface AlignmentGuide {
  type: 'vertical' | 'horizontal';
  position: number; // x 坐标（垂直线）或 y 坐标（水平线）
  start: number;    // 线的起始坐标
  end: number;      // 线的结束坐标
}

const GRID_SIZE = 8;
const ALIGN_THRESHOLD = 4; // 像素

/**
 * 网格吸附：将坐标对齐到网格。
 */
export function snapToGrid(value: number, gridSize: number = GRID_SIZE): number {
  return Math.round(value / gridSize) * gridSize;
}

/**
 * 检测边缘对齐并返回对齐后的坐标和辅助线。
 */
export function detectAlignment(
  current: BlockBounds,
  others: BlockBounds[],
  altPressed: boolean,
): AlignmentResult {
  // Alt 键按下时禁用吸附
  if (altPressed) {
    return {
      position: { x: current.x, y: current.y },
      guides: [],
    };
  }

  const guides: AlignmentGuide[] = [];
  let alignedX = snapToGrid(current.x);
  let alignedY = snapToGrid(current.y);

  // 检测与其他 Block 的边缘对齐
  for (const other of others) {
    if (other.id === current.id) continue;

    // 左边缘对齐
    if (Math.abs(current.x - other.x) <= ALIGN_THRESHOLD) {
      alignedX = other.x;
      guides.push({
        type: 'vertical',
        position: other.x,
        start: Math.min(current.y, other.y),
        end: Math.max(current.y + current.height, other.y + other.height),
      });
    }

    // 右边缘对齐（当前块的右边 = 其他块的右边）
    const currentRight = current.x + current.width;
    const otherRight = other.x + other.width;
    if (Math.abs(currentRight - otherRight) <= ALIGN_THRESHOLD) {
      alignedX = otherRight - current.width;
      guides.push({
        type: 'vertical',
        position: otherRight,
        start: Math.min(current.y, other.y),
        end: Math.max(current.y + current.height, other.y + other.height),
      });
    }

    // 当前块右边 = 其他块左边
    if (Math.abs(currentRight - other.x) <= ALIGN_THRESHOLD) {
      alignedX = other.x - current.width;
      guides.push({
        type: 'vertical',
        position: other.x,
        start: Math.min(current.y, other.y),
        end: Math.max(current.y + current.height, other.y + other.height),
      });
    }

    // 当前块左边 = 其他块右边
    if (Math.abs(current.x - otherRight) <= ALIGN_THRESHOLD) {
      alignedX = otherRight;
      guides.push({
        type: 'vertical',
        position: otherRight,
        start: Math.min(current.y, other.y),
        end: Math.max(current.y + current.height, other.y + other.height),
      });
    }

    // 上边缘对齐
    if (Math.abs(current.y - other.y) <= ALIGN_THRESHOLD) {
      alignedY = other.y;
      guides.push({
        type: 'horizontal',
        position: other.y,
        start: Math.min(current.x, other.x),
        end: Math.max(current.x + current.width, other.x + other.width),
      });
    }

    // 下边缘对齐
    const currentBottom = current.y + current.height;
    const otherBottom = other.y + other.height;
    if (Math.abs(currentBottom - otherBottom) <= ALIGN_THRESHOLD) {
      alignedY = otherBottom - current.height;
      guides.push({
        type: 'horizontal',
        position: otherBottom,
        start: Math.min(current.x, other.x),
        end: Math.max(current.x + current.width, other.x + other.width),
      });
    }

    // 当前块底边 = 其他块顶边
    if (Math.abs(currentBottom - other.y) <= ALIGN_THRESHOLD) {
      alignedY = other.y - current.height;
      guides.push({
        type: 'horizontal',
        position: other.y,
        start: Math.min(current.x, other.x),
        end: Math.max(current.x + current.width, other.x + other.width),
      });
    }

    // 当前块顶边 = 其他块底边
    if (Math.abs(current.y - otherBottom) <= ALIGN_THRESHOLD) {
      alignedY = otherBottom;
      guides.push({
        type: 'horizontal',
        position: otherBottom,
        start: Math.min(current.x, other.x),
        end: Math.max(current.x + current.width, other.x + other.width),
      });
    }
  }

  // 去重辅助线
  const uniqueGuides = deduplicateGuides(guides);

  return {
    position: { x: alignedX, y: alignedY },
    guides: uniqueGuides,
  };
}

/**
 * 去重辅助线（相同 type + position 的合并）。
 */
function deduplicateGuides(guides: AlignmentGuide[]): AlignmentGuide[] {
  const map = new Map<string, AlignmentGuide>();
  for (const g of guides) {
    const key = `${g.type}-${g.position}`;
    const existing = map.get(key);
    if (existing) {
      existing.start = Math.min(existing.start, g.start);
      existing.end = Math.max(existing.end, g.end);
    } else {
      map.set(key, { ...g });
    }
  }
  return Array.from(map.values());
}

/**
 * React Hook：useBlockAlignment
 * 
 * 用法：
 * const { snappedPosition, guides, isAltPressed } = useBlockAlignment(
 *   currentBlock,
 *   allBlocks,
 *   isDragging,
 * );
 */
import { useState, useEffect, useCallback } from 'react';

export function useBlockAlignment(
  currentBlock: BlockBounds | null,
  allBlocks: BlockBounds[],
  isDragging: boolean,
): AlignmentResult & { isAltPressed: boolean } {
  const [isAltPressed, setIsAltPressed] = useState(false);
  const [result, setResult] = useState<AlignmentResult>({
    position: { x: 0, y: 0 },
    guides: [],
  });

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Alt') {
        setIsAltPressed(true);
        e.preventDefault();
      }
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Alt') {
        setIsAltPressed(false);
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('keyup', handleKeyUp);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('keyup', handleKeyUp);
    };
  }, []);

  const computeAlignment = useCallback(() => {
    if (!currentBlock || !isDragging) {
      setResult({ position: { x: currentBlock?.x ?? 0, y: currentBlock?.y ?? 0 }, guides: [] });
      return;
    }
    const aligned = detectAlignment(currentBlock, allBlocks, isAltPressed);
    setResult(aligned);
  }, [currentBlock, allBlocks, isDragging, isAltPressed]);

  useEffect(() => {
    computeAlignment();
  }, [computeAlignment]);

  return { ...result, isAltPressed };
}
