# GCS to Kubernetes Deployment Manager

This project is a Python-based deployment manager that automates the deployment of applications to a Kubernetes cluster from Google Cloud Storage (GCS). It continuously monitors a GCS bucket for new application packages (`.tar.gz` files), and upon detection, it triggers a build and deployment pipeline using Kaniko and Kubernetes.

## Table of Contents

- [GCS to Kubernetes Deployment Manager](#gcs-to-kubernetes-deployment-manager)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Features](#features)
  - [Prerequisites](#prerequisites)
  - [Configuration](#configuration)
  - [Usage](#usage)
  - [Deployment](#deployment)
  - [Built With](#built-with)

## Overview

The deployment manager is designed to streamline the process of deploying applications to Kubernetes. It watches a specified GCS bucket for new application packages. When a new package is uploaded, the manager automatically:

1.  **Validates the package**: Checks for the presence of a `Dockerfile`.
2.  **Builds a container image**: Uses Kaniko to build a Docker image from the application source code.
3.  **Pushes the image to a container registry**: Stores the newly built image in a specified container registry.
4.  **Deploys the application to Kubernetes**: Creates or updates the necessary Kubernetes resources (Deployment, Service, and Ingress) to run the application.

## Features

- **Automated deployments**: Automatically deploys new applications from GCS to Kubernetes.
- **Container image builds**: Uses Kaniko to build container images without requiring a Docker daemon.
- **Kubernetes integration**: Manages Kubernetes resources (Deployments, Services, Ingresses) for the application.
- **Scalable**: Can be configured to watch multiple applications and environments.
- **Extensible**: Can be extended to support other application packaging formats and deployment strategies.

## Prerequisites

Before you begin, ensure you have the following:

- A Google Cloud Platform (GCP) project with the following APIs enabled:
  - Google Cloud Storage API
  - Google Kubernetes Engine API
- A GCS bucket to store your application packages.
- A container registry (e.g., Google Container Registry) to store your container images.
- A running Kubernetes cluster.
- `kubectl` configured to connect to your Kubernetes cluster.

## Configuration

The deployment manager is configured using environment variables. Create a `.env` file in the `manager` directory with the following variables:

```
GCS_BUCKET_NAME="your-gcs-bucket-name"
CONTAINER_REGISTRY_URL="your-container-registry-url"
K8S_NAMESPACE="your-kubernetes-namespace"
BUILD_SERVICE_ACCOUNT_NAME="your-build-service-account"
DOMAIN_NAME="your-domain.com"
```

- `GCS_BUCKET_NAME`: The name of the GCS bucket to watch for new application packages.
- `CONTAINER_REGISTRY_URL`: The URL of the container registry where the built images will be stored.
- `K8S_NAMESPACE`: The Kubernetes namespace where the applications will be deployed.
- `BUILD_SERVICE_ACCOUNT_NAME`: The name of the Kubernetes service account to use for the build job.
- `DOMAIN_NAME`: The domain name to use for the Ingress resource.

## Usage

To run the deployment manager, you need to have Python and the required dependencies installed.

1.  **Install the dependencies**:

```
pip install -r manager/requirements.txt
```

2.  **Run the deployment manager**:

```
python manager/main.py
```

The deployment manager will start watching the specified GCS bucket for new application packages.

## Deployment

To deploy the deployment manager to your Kubernetes cluster, you can use the provided `deployment.yaml` file as a template. You will need to build a Docker image for the deployment manager and push it to your container registry.

1.  **Build the Docker image**:

```
docker build -t your-container-registry-url/gcs-to-k8s-deployment-manager:latest .
```

2.  **Push the Docker image**:

```
docker push your-container-registry-url/gcs-to-k8s-deployment-manager:latest
```

3.  **Deploy to Kubernetes**:

```
kubectl apply -f manager/deployment.yaml
```

## Built With

- [Python](https://www.python.org/)
- [Google Cloud Storage](https://cloud.google.com/storage)
- [Kubernetes](https://kubernetes.io/)
- [Kaniko](https://github.com/GoogleContainerTools/kaniko)
- [Docker](https://www.docker.com/)
