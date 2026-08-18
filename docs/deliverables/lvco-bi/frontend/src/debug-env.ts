console.log("[DEBUG] VITE_API_BASE_URL =", import.meta.env.VITE_API_BASE_URL);
console.log("[DEBUG] all env keys =", Object.keys(import.meta.env).filter(k => k.includes("API") || k.includes("VITE")));