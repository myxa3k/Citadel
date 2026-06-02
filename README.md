Citadel — name of my first home-lab. Here I'm going to document everything — how everything was set up, which services are in use, and what issue I encountered. 

**Birthday**: 28.05.26

# Server Speсifications

## Hardware Components
 * ***CPU**: AMD Epyc 7313
 * ***RAM**: MICRON 16GBx8 2RX8 DDR4-2666MHZ RDIMM
 * ***Storage**: 
	-  HDD 8TB Seagate
	-  HDD 8TB Western Digital Purple
	-  SSD 1TB Kingston KC3000
 * ***Motherboard**: South H12D-8D
 * ***PSU**: be quiet! Pure Power 13 M 1000 Watt

## Infrastructure & Services

### Hypervisor (Proxmox VE)
* **Node 1**: Main cluster node

### Virtual Machines (VMs) & Containers (LXC)
*  **Streaming VM**: 
	* Jellyfin — Media server for home streaming. 
	* Seerr — Request management tool for movies and TV shows. 
	* Sonarr — Smart TV show downloader and automation tool. 
	* Prowlarr — Indexer manager for torrent trackers. 
	* qBittorrent — Torrent client for automated media downloading.
*  **Games VM**: 
	* Pterodactyl — Open-source game server management panel. 
*  **Drive VM**: 
	* Immich — Self-hosted photo and video backup solution.
	* Nextcloud — Private cloud storage and file sharing platform.
*  **Homepage LXC**: 
	* Homepage — Centralized dashboard for linking and monitoring all home-lab services.
	