variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "ID of the VPC where all resources will be created."
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for ECS tasks, RDS, and ElastiCache."
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs for the Application Load Balancer."
  type        = list(string)
}

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

variable "llm_base_url" {
  description = "Base URL for the LLM provider API."
  type        = string
}

variable "openai_api_key" {
  description = "API key for the LLM provider."
  type        = string
  sensitive   = true
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "production"
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "turnstone"
}

variable "auth_token" {
  description = "Optional authentication token for the Turnstone API."
  type        = string
  sensitive   = true
  default     = ""
}
