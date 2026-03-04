terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

module "turnstone" {
  source = "../../modules/aws-ecs"

  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids
  public_subnet_ids  = var.public_subnet_ids

  image_repository = var.image_repository
  image_tag        = var.image_tag

  llm_base_url   = var.llm_base_url
  openai_api_key = var.openai_api_key

  environment = var.environment
  name_prefix = var.name_prefix
  auth_token  = var.auth_token

  tags = {
    Example = "aws-ecs-basic"
  }
}
