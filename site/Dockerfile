FROM node:22-alpine AS build
WORKDIR /src
# the docs sync script reads ../docs, so the build context is the repository root
COPY . .
WORKDIR /src/site
ENV BASE_PATH=/ SITE_URL=http://127.0.0.1:8088
RUN npm ci && npm run build

FROM nginx:alpine
COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/site/dist /usr/share/nginx/html
EXPOSE 80
