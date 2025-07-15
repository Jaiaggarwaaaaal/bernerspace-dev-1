import os
import argparse
from google.cloud import storage
from google.auth import exceptions as auth_exceptions
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def upload_to_gcs(file_path, bucket_name, blob_name=None):
    """
    Uploads a file to a Google Cloud Storage bucket.

    :param file_path: Path to the file to upload.
    :param bucket_name: The name of the GCS bucket.
    :param blob_name: The GCS blob name (object name). If not specified, the file_path's basename is used.
    :return: True if the file was uploaded successfully, else False.
    """
    # --- Initialize Client ---
    try:
        storage_client = storage.Client()
    except auth_exceptions.DefaultCredentialsError:
        print("Error: Google Cloud credentials not found.")
        print("Please ensure GOOGLE_APPLICATION_CREDENTIALS is set correctly in your .env file.")
        return False

    # --- Validate Inputs ---
    if not os.path.exists(file_path):
        print(f"Error: The file '{file_path}' does not exist.")
        return False

    if blob_name is None:
        blob_name = os.path.basename(file_path)

    print(f"Uploading '{file_path}' to bucket '{bucket_name}' as '{blob_name}'...")

    # --- Perform Upload ---
    try:
        bucket = storage_client.bucket(bucket_name)
        if not bucket.exists():
            print(f"Error: The bucket '{bucket_name}' does not exist or you don't have access to it.")
            return False

        blob = bucket.blob(blob_name)
        blob.upload_from_filename(file_path)

        print(f"Successfully uploaded '{blob_name}' to '{bucket_name}'.")
        return True
    except exceptions.Forbidden as e:
        print(f"Error: Permission denied. Check your service account permissions for GCS.")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during upload: {e}")
        return False

def main():
    """Main function to parse arguments and initiate the upload."""
    parser = argparse.ArgumentParser(description="Upload a ZIP file to Google Cloud Storage.")
    parser.add_argument("file_path", type=str, help="The path to the ZIP file to upload.")
    
    args = parser.parse_args()

    bucket_name = os.getenv('GCS_BUCKET_NAME')

    if not bucket_name:
        print("Error: GCS_BUCKET_NAME is not set in the .env file.")
        return

    upload_to_gcs(args.file_path, bucket_name)

if __name__ == "__main__":
    main()
