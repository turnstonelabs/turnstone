# --- Networking ---

variable "vpc_id" {
  description = "ID of the VPC where all resources will be created."
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for ECS tasks and RDS."
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs for the Application Load Balancer."
  type        = list(string)
}

# --- Container Image ---

variable "image_repository" {
  description = "Container image repository."
  type        = string
  default     = "ghcr.io/turnstonelabs/turnstone"
}

variable "image_tag" {
  description = "Container image tag."
  type        = string
  default     = "latest"
}

# --- LLM Provider ---

variable "llm_base_url" {
  description = "Base URL for the LLM provider API (e.g. https://api.openai.com/v1)."
  type        = string
}

variable "openai_api_key" {
  description = "API key for the LLM provider. Stored in AWS Secrets Manager."
  type        = string
  sensitive   = true
}

# --- RDS ---

variable "db_instance_class" {
  description = "RDS instance class for PostgreSQL."
  type        = string
  default     = "db.t4g.micro"
}

# --- ECS Task Sizing ---

variable "server_cpu" {
  description = "CPU units for the server task (1 vCPU = 1024)."
  type        = number
  default     = 512
}

variable "server_memory" {
  description = "Memory (MiB) for the server task."
  type        = number
  default     = 1024
}

variable "console_cpu" {
  description = "CPU units for the console task."
  type        = number
  default     = 256
}

variable "console_memory" {
  description = "Memory (MiB) for the console task."
  type        = number
  default     = 512
}

# --- General ---

variable "environment" {
  description = "Deployment environment name (e.g. production, staging)."
  type        = string
  default     = "production"
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "turnstone"
}

variable "auth_token" {
  description = "Optional authentication token for the Turnstone API. Empty string disables auth."
  type        = string
  sensitive   = true
  default     = ""
}

variable "certificate_arn" {
  description = "ACM certificate ARN for HTTPS listeners. Leave empty for HTTP-only (not recommended for production)."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
