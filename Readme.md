# GCS-to-Kubernetes Deployment Manager

This project implements a Kubernetes-native controller that automates the deployment of applications to a GKE cluster directly from Google Cloud Storage. It watches a GCS bucket for new application source code (in `.tar` or `.tar.gz` format), builds a Docker image using Kaniko, pushes it to a container registry, and deploys it using a set of predefined Kubernetes templates.

## Features

- **GitOps-style Trigger**: Deploy applications by simply uploading a tarball to a GCS bucket.
- **Automated Builds**: Uses Kaniko to build container images from a `Dockerfile` within your source code, without needing a Docker daemon.
- **Dynamic Naming**: The application name and version are derived directly from the GCS file path (e.g., `my-app/v1.2.tar` becomes application `my-app` with version `v1.2`).
- **Templated Deployments**: Creates `Deployment`, `Service`, and `Ingress` resources from standardized templates.
- **Automatic SSL**: Integrates with `cert-manager` to automatically provision and manage SSL certificates for deployed applications.
- **Secure**: Leverages GKE Workload Identity for secure, keyless authentication between the manager and other Google Cloud services.

## Architecture

The deployment process follows these steps:

1.  A user uploads an application tarball to the configured GCS bucket (e.g., `gs://<your-bucket>/my-fastapi-app/v1.0.tar`).
2.  The `gcs-deployment-manager` pod, which is constantly polling the bucket, detects the new file.
3.  The manager extracts the application name (`my-fastapi-app`) and version (`v1.0`) from the file path.
4.  It creates a Kubernetes `Job` to run a Kaniko build pod.
5.  The Kaniko pod fetches the tarball from GCS, builds the Docker image using the `Dockerfile` found inside, and pushes the final image to the configured container registry with the tag `my-fastapi-app:v1.0`.
6.  The manager pod monitors the build `Job`. Upon successful completion, it proceeds to the deployment phase.
7.  Using templates, it generates manifests for a `Deployment`, `Service`, and `Ingress`.
8.  It applies these manifests to the cluster, creating the necessary resources to run the application and expose it to the internet.
9.  The `cert-manager` installation detects the new `Ingress` and automatically obtains a TLS certificate for its domain.

```
+----------------+      +----------------------+      +-------------------------+
|   User / CI    |----->|      GCS Bucket      |----->| gcs-deployment-manager  |
+----------------+  1.  +----------------------+  2.  +-------------------------+
     Uploads Tarball      New file detected           (Pod running in GKE)
                                                                 | 3. Creates Job
                                                                 v
                                                     +-------------------------+
                                                     |      Kaniko Build Job   |
                                                     +-------------------------+
                                                                 | 4. Pushes Image
                                                                 v
                                                     +-------------------------+
                                                     |  Container Registry     |
                                                     +-------------------------+
                                                                 | 5. Reports Success
                                                                 v
+-------------------------+      +-------------------------+      +-------------------------+
| Ingress                 |<-----| Service                 |<-----| Deployment              |
+-------------------------+  8.  +-------------------------+  7.  +-------------------------+
  (Exposes App via HTTPS)        (Routes traffic)                 (Runs App Pod)
```

## Prerequisites

- A Google Kubernetes Engine (GKE) cluster with **Workload Identity enabled**.
- A Google Cloud Storage (GCS) bucket.
- A container registry (e.g., Google Artifact Registry).
- A configured domain name pointing to the IP address of your GKE cluster's Ingress controller.
- `cert-manager` installed on your cluster.
- A Google Service Account (GSA) with the following IAM roles:
  - **Storage Object Viewer** (`roles/storage.objectViewer`): To read files from the GCS bucket.
  - **Artifact Registry Writer** (`roles/artifactregistry.writer`): To push container images.
  - **Kubernetes Engine Developer** (`roles/container.developer`): To manage Kubernetes resources.
  - **Service Account User** (`roles/iam.serviceAccountUser`): To allow the GKE service account to impersonate the GSA.
  - **Workload Identity User** (`roles/iam.workloadIdentityUser`): To link the GSA to the Kubernetes Service Account.

## Setup

1.  **Clone the Repository**:
    ```bash
    git clone <repository-url>
    cd gcs-k8s-deployment-manager
    ```

2.  **Configure Service Accounts**:
    - Ensure your Google Service Account (GSA) has the required permissions (see Prerequisites).
    - Link the GSA to the Kubernetes Service Account (KSA) that the manager will use (`bspace` in the `bspacekubs` namespace by default).
    ```bash
    gcloud iam service-accounts add-iam-policy-binding \
      --role="roles/iam.workloadIdentityUser" \
      --member="serviceAccount:<your-project-id>.svc.id.goog[bspacekubs/bspace]" \
      <your-gsa-email>
    ```

3.  **Configure Deployment Files**:
    - **`manager/manager-deployment.yaml`**: This file defines the manager's deployment, roles, and service account usage. It is pre-configured to use a KSA named `bspace`. Ensure the RBAC roles meet your security requirements.
    - **`manager/templates/`**: Review the `deployment.yaml`, `service.yaml`, and `ingress.yaml` templates. Crucially, ensure the `targetPort` in `service.yaml` matches the port your applications will listen on (e.g., `8080` for FastAPI/Uvicorn).

4.  **Configure CI/CD Secrets**:
    - In your GitHub repository, go to `Settings > Secrets and variables > Actions` and create secrets for the values used in `.github/workflows/deploy.yml`, such as `GCP_CREDENTIALS`, `GKE_CLUSTER_NAME`, `CONTAINER_REGISTRY_URL`, etc.

5.  **Deploy the Manager**:
    - Pushing changes to the `main` or `deploy` branch will trigger the GitHub Actions workflow.
    - The workflow builds the manager's Docker image, pushes it to your registry, and applies the `manager/manager-deployment.yaml` manifest to your cluster.

## Usage

To deploy a new application:

1.  **Prepare Your Application**:
    - Your application source code **must** contain a valid `Dockerfile` at its root.
    - Ensure your application is configured to listen on the port specified in the `service.yaml` template (e.g., `8080`), keep your code port 8080 by default to use this with our cli

2.  **Create a Tarball**:
    - Create a `.tar` or `.tar.gz` archive of your application's source code.
    ```bash
    tar -cvf my-app/v1.0.tar .
    # or
    tar -zcvf my-app/v1.0.tar.gz .
    ```

3.  **Upload to GCS**:
    - Upload the file to your GCS bucket using the required path structure: `<app-name>/<version>.<ext>`.
    ```bash
    gsutil cp my-app/v1.0.tar gs://<your-bucket>/my-app/v1.0.tar
    ```
    - The manager will automatically detect the new file and begin the build and deploy process.

4.  **Verify Deployment**:
    - You can watch the process in your cluster:
    ```bash
    # See the build job get created, and then your application pod
    kubectl get pods -n bspacekubs -w

    # Check for the final deployed resources
    kubectl get deployment,service,ingress -n bspacekubs
    ```
    - Once the `Ingress` is available and the certificate is ready, your application will be accessible at `https://<app-name>-<version>.<your-domain.com>`.

## Demo Video
[![Demo Video](https://img.youtube.com/vi/SN57P-qaBzI/0.jpg)](https://www.youtube.com/watch?v=SN57P-qaBzI)
