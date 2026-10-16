# Rename stocktale -> tenetfolio on the VM (do this with the next deploy)

The code in the repo is already renamed (localStorage keys keep a fallback to the old names, so no user loses saved views).
The VM still runs a folder `~/stocktale` and a container `stocktale`; Caddy reaches it by that name. Rename in this order:

```bash
K=~/Downloads/ssh-key-2026-08-15.key
ssh -i $K ubuntu@152.67.9.111 'set -e
cd ~/stocktale
cp docker-compose.yml docker-compose.yml.bak-prerename
sed -i "s/^  stocktale:/  tenetfolio:/; s/container_name: stocktale/container_name: tenetfolio/" docker-compose.yml
sudo docker compose down --remove-orphans
cd ~ && mv stocktale tenetfolio && cd tenetfolio
cp ~/munshi/Caddyfile ~/munshi/Caddyfile.bak-prerename
sed -i "s/reverse_proxy stocktale:8000/reverse_proxy tenetfolio:8000/" ~/munshi/Caddyfile
sudo docker compose up -d --build
sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile'
```

Then change the deploy rsync target from `~/stocktale/` to `~/tenetfolio/`, and check `curl -I https://tenetfolio.com/login` returns 200.
Downtime is the build/start (1 to 3 minutes). Rollback: restore both `.bak-prerename` files, move the folder back, `docker compose up -d`.
Do not touch the `stocktale.linkpc.net` block in the Caddyfile: that is the old web address, kept as a redirect.
