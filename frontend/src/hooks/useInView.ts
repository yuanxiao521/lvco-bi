import { useRef, useState, useEffect, useCallback } from 'react'

interface UseInViewOptions {
  threshold?: number
  rootMargin?: string
}

export function useInView(options: UseInViewOptions = {}) {
  const { threshold = 0.1, rootMargin = '100px' } = options
  const ref = useRef<HTMLDivElement | null>(null)
  const [inView, setInView] = useState(false)

  const setRef = useCallback((node: HTMLDivElement | null) => {
    ref.current = node
  }, [])

  useEffect(() => {
    const node = ref.current
    if (!node) return

    // On platforms without IntersectionObserver (SSR/CI), default to visible
    if (typeof IntersectionObserver === 'undefined') {
      setInView(true)
      return
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true)
          observer.disconnect()
        }
      },
      { threshold, rootMargin }
    )

    observer.observe(node)
    return () => observer.disconnect()
  }, [threshold, rootMargin])

  return { ref: setRef, inView }
}
