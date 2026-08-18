import { snapToGrid, detectAlignment } from '../useBlockAlignment';
import type { BlockBounds } from '../useBlockAlignment';

describe('snapToGrid', () => {
  it('should snap to nearest grid point', () => {
    expect(snapToGrid(0)).toBe(0);
    expect(snapToGrid(3)).toBe(0);
    expect(snapToGrid(4)).toBe(8);
    expect(snapToGrid(7)).toBe(8);
    expect(snapToGrid(8)).toBe(8);
    expect(snapToGrid(12)).toBe(16);
  });

  it('should support custom grid size', () => {
    expect(snapToGrid(7, 10)).toBe(10);
    expect(snapToGrid(3, 10)).toBe(0);
  });
});

describe('detectAlignment', () => {
  const makeBlock = (id: string, x: number, y: number, w = 100, h = 100): BlockBounds => ({
    id, x, y, width: w, height: h,
  });

  it('should return original position when alt is pressed', () => {
    const current = makeBlock('a', 13, 27);
    const others = [makeBlock('b', 0, 0)];
    const result = detectAlignment(current, others, true);
    expect(result.position.x).toBe(13);
    expect(result.position.y).toBe(27);
    expect(result.guides).toHaveLength(0);
  });

  it('should snap to grid when no alignment', () => {
    const current = makeBlock('a', 13, 27);
    const others: BlockBounds[] = [];
    const result = detectAlignment(current, others, false);
    expect(result.position.x).toBe(16); // snapToGrid(13) = 16
    expect(result.position.y).toBe(24); // snapToGrid(27) = 24
  });

  it('should detect left edge alignment', () => {
    const current = makeBlock('a', 52, 200); // x=52, close to 50
    const others = [makeBlock('b', 50, 0)];
    const result = detectAlignment(current, others, false);
    expect(result.position.x).toBe(50);
    expect(result.guides.some(g => g.type === 'vertical' && g.position === 50)).toBe(true);
  });

  it('should detect right-to-left alignment', () => {
    // current right edge (100+100=200) close to other left edge (203)
    const current = makeBlock('a', 100, 0, 100, 100);
    const others = [makeBlock('b', 203, 0)];
    const result = detectAlignment(current, others, false);
    // current.x should be adjusted so currentRight = 203
    expect(result.position.x).toBe(103);
  });

  it('should detect top edge alignment', () => {
    const current = makeBlock('a', 200, 53);
    const others = [makeBlock('b', 0, 50)];
    const result = detectAlignment(current, others, false);
    expect(result.position.y).toBe(50);
    expect(result.guides.some(g => g.type === 'horizontal' && g.position === 50)).toBe(true);
  });

  it('should deduplicate guides', () => {
    const current = makeBlock('a', 50, 50);
    const others = [
      makeBlock('b', 50, 0),
      makeBlock('c', 50, 200),
    ];
    const result = detectAlignment(current, others, false);
    const verticalGuides = result.guides.filter(g => g.type === 'vertical' && g.position === 50);
    expect(verticalGuides).toHaveLength(1);
  });
});
