FROM node:22-alpine

WORKDIR /app

COPY package.json ./
COPY tsconfig.json ./
COPY vite.config.ts ./
COPY index.html ./

RUN npm install

COPY src ./src
COPY public ./public

# 容器内由外层 nginx 提供 TLS 与 /api/ 路由，前端必须用同源相对路径，
# 否则浏览器会按 mixed content 拦截 http:// 请求（数据库密码也就走了明文）
ENV VITE_API_BASE_URL=/api/v1

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "3000"]