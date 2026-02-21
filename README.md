# Netcup DNS Manager

Combined DDNS updater + DNS record cleanup for Netcup domains. Supports multiple domains in a single container.

## Features
- **DDNS**: Keeps A-records in sync with your WAN IPv4
- **Cleanup**: Removes duplicate A-records automatically
- **Multi-domain**: Manage multiple domains with one container
- **Lightweight**: Python 3.13, ~30MB image

## Quick Start
```bash
cp .env.example .env
# Edit .env with your credentials
docker compose up -d
```

## Configuration

### Multi-domain (recommended)
```
DOMAINS=sydrv.de:*,@;fm-p.de:*,@
NETCUP_CUSTOMER_ID=123456
NETCUP_API_KEY=xxx
NETCUP_API_PASSWORD=yyy
```

Format: `domain:hosts:prefix` separated by `;`
- `hosts`: comma-separated hostnames to update (`*,@`)
- `prefix`: optional credential prefix for per-domain credentials

### Per-domain credentials
```
DOMAINS=domain1.de:*,@:D1;domain2.de:*,@:D2
D1_CUSTOMER_ID=111
D1_API_KEY=xxx
D1_API_PASSWORD=yyy
D2_CUSTOMER_ID=222
D2_API_KEY=xxx
D2_API_PASSWORD=yyy
```

### Legacy single-domain
```
NETCUP_DOMAIN_NAME=example.de
ZONE_HOSTS=*,@
NETCUP_CUSTOMER_ID=123456
NETCUP_API_KEY=xxx
NETCUP_API_PASSWORD=yyy
```
