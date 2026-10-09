/**
 * 对齐辅助线渲染组件。
 * 
 * 在画布上渲染 SVG 虚线，指示 Block 对齐位置。
 */
import React from 'react';
import type { AlignmentGuide } from '../../hooks/useBlockAlignment';

interface AlignmentGuidesProps {
  guides: AlignmentGuide[];
  canvasWidth: number;
  canvasHeight: number;
}

export const AlignmentGuides: React.FC<AlignmentGuidesProps> = ({
  guides,
  canvasWidth,
  canvasHeight,
}) => {
  if (guides.length === 0) return null;

  return (
    <svg
      className="absolute inset-0 pointer-events-none z-50"
      width={canvasWidth}
      height={canvasHeight}
      style={{ overflow: 'visible' }}
    >
      {guides.map((guide, i) => {
        if (guide.type === 'vertical') {
          return (
            <line
              key={`v-${i}`}
              x1={guide.position}
              y1={guide.start}
              x2={guide.position}
              y2={guide.end}
              stroke="#3b82f6"
              strokeWidth={1}
              strokeDasharray="4 2"
              opacity={0.8}
            />
          );
        } else {
          return (
            <line
              key={`h-${i}`}
              x1={guide.start}
              y1={guide.position}
              x2={guide.end}
              y2={guide.position}
              stroke="#3b82f6"
              strokeWidth={1}
              strokeDasharray="4 2"
              opacity={0.8}
            />
          );
        }
      })}
    </svg>
  );
};
