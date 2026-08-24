variable "region" {
  description = "AWS region to deploy the demo stack into."
  type        = string
  default     = "us-east-1"
}

variable "db_password" {
  description = "Master password for the demo Postgres instance."
  type        = string
  default     = "changeme-in-real-life"
  sensitive   = true
}

locals {
  tags = {
    Project     = "itest-demo"
    Environment = "demo"
    ManagedBy   = "terraform"
  }
}
