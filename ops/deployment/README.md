# 生产发布基座

`docker-compose.prod.yml` 是服务器 `/root/happyvalley-deploy/deploy/docker-compose.prod.yml` 的受版本控制副本。它将后端 `/app/media` 挂载到宿主机 `data/media`，发布重建容器不会删除用户媒体。

首次初始化由生产管理员在服务器执行：

```bash
bash install-server-base.sh <github-actions-deploy-public-key>
```

脚本创建受限 `deploy` 用户；该用户只能接收镜像、发布指定服务或回滚，不能取得交互式终端。GitHub Environment `production` 需要配置：`DEPLOY_SSH_PRIVATE_KEY`、`DEPLOY_SSH_KNOWN_HOSTS`、`DEPLOY_SSH_HOST`、`DEPLOY_SSH_USER`。

开发者日常只在对应仓库的 Actions 页面运行 `Deploy Frontend Production` 或 `Deploy Backend Production`。回滚工作流的 commit 参数是服务器发布记录中保存的 `previous_commit`。
