# Scalable Incident Platform

## Deploy on Azure VM

Follow these steps after the Azure VM and Docker environment have been set up.

### 1. Clone the Private Repository

Clone the project repository:

```bash
git clone git@github.com:RameshMalar/scalable-incident-platform.git
```

Enter the project directory:

```bash
cd scalable-incident-platform
```

---

### 2. Login to GitHub Container Registry

The application Docker image is stored in the private GitHub Container Registry (GHCR).

Set your own GitHub username:

```bash
export GITHUB_USER=<YOUR_GITHUB_USERNAME>
```

Set your GitHub Personal Access Token:

```bash
export CR_PAT=<YOUR_GITHUB_PERSONAL_ACCESS_TOKEN>
```

Login to GitHub Container Registry:

```bash
echo $CR_PAT | docker login ghcr.io -u $GITHUB_USER --password-stdin
```

Expected result:

```text
Login Succeeded
```

> Use your own GitHub credentials.  
> Do not store or commit Personal Access Tokens in this repository.

---

### 3. Start the Application

Run:

```bash
docker compose up -d
```

Docker Compose will automatically:

- Pull the required Docker image from GHCR
- Create the container
- Configure the required port
- Start the application

---

### 4. Verify the Application

Check the running container:

```bash
docker ps
```

Test the health endpoint:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{
  "status": "healthy",
  "service": "incident-ingestion-service"
}
```

---

### 5. Access the Application

Dashboard:

```text
http://<VM-PUBLIC-IP>:8000/
```

Swagger API:

```text
http://<VM-PUBLIC-IP>:8000/docs
```

---

### 6. Stop the Application

To stop and remove the running containers:

```bash
docker compose down
```

---

### 7. Pull Future Updates

Pull the latest repository changes:

```bash
git pull origin main
```

Pull the Docker image configured in Docker Compose:

```bash
docker compose pull
```

Start or update the application:

```bash
docker compose up -d
```

---

## Quick Deployment

After the initial GitHub/GHCR authentication is configured, deployment is:

```bash
git pull origin main
docker compose pull
docker compose up -d
```