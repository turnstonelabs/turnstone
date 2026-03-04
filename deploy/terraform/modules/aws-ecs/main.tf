terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

locals {
  full_image = "${var.image_repository}:${var.image_tag}"

  common_tags = merge(var.tags, {
    Project     = "turnstone"
    Environment = var.environment
    ManagedBy   = "terraform"
  })

  # Shared environment variables injected into every container.
  common_env = [
    { name = "TURNSTONE_ENV", value = var.environment },
    { name = "TURNSTONE_DB_BACKEND", value = "postgresql" },
    { name = "TURNSTONE_LLM_BASE_URL", value = var.llm_base_url },
    { name = "TURNSTONE_REDIS_URL", value = "redis://${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0" },
  ]

  # Secrets pulled from Secrets Manager at container start.
  common_secrets = [
    {
      name      = "OPENAI_API_KEY"
      valueFrom = aws_secretsmanager_secret_version.openai_api_key.arn
    },
    {
      name      = "TURNSTONE_DB_URL"
      valueFrom = aws_secretsmanager_secret_version.db_url.arn
    },
  ]

  auth_env = var.auth_token != "" ? [
    { name = "TURNSTONE_AUTH_ENABLED", value = "true" },
  ] : []

  auth_secrets = var.auth_token != "" ? [
    {
      name      = "TURNSTONE_AUTH_TOKEN"
      valueFrom = aws_secretsmanager_secret_version.auth_token[0].arn
    },
  ] : []
}

# ---------- Secrets Manager ----------

resource "aws_secretsmanager_secret" "openai_api_key" {
  name = "${var.name_prefix}-${var.environment}-openai-api-key"
  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "openai_api_key" {
  secret_id     = aws_secretsmanager_secret.openai_api_key.id
  secret_string = var.openai_api_key
}

resource "aws_secretsmanager_secret" "auth_token" {
  count = var.auth_token != "" ? 1 : 0
  name  = "${var.name_prefix}-${var.environment}-auth-token"
  tags  = local.common_tags
}

resource "aws_secretsmanager_secret_version" "auth_token" {
  count         = var.auth_token != "" ? 1 : 0
  secret_id     = aws_secretsmanager_secret.auth_token[0].id
  secret_string = var.auth_token
}

resource "aws_secretsmanager_secret" "db_password" {
  name = "${var.name_prefix}-${var.environment}-db-password"
  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = random_password.db.result
}

resource "aws_secretsmanager_secret" "db_url" {
  name = "${var.name_prefix}-${var.environment}-db-url"
  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "db_url" {
  secret_id     = aws_secretsmanager_secret.db_url.id
  secret_string = "postgresql+psycopg://${aws_db_instance.this.username}:${random_password.db.result}@${aws_db_instance.this.endpoint}/turnstone"
}

# ---------- ECS Cluster ----------

resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-${var.environment}"
  tags = local.common_tags

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------- CloudWatch Log Group ----------

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name_prefix}-${var.environment}"
  retention_in_days = 30
  tags              = local.common_tags
}

# ---------- Server Task Definition + Service ----------

resource "aws_ecs_task_definition" "server" {
  family                   = "${var.name_prefix}-server"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.server_cpu
  memory                   = var.server_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  tags                     = local.common_tags

  container_definitions = jsonencode([
    {
      name      = "server"
      image     = local.full_image
      essential = true
      command   = ["turnstone-server", "--host", "0.0.0.0", "--port", "8080"]

      portMappings = [
        { containerPort = 8080, protocol = "tcp" },
      ]

      environment = concat(local.common_env, local.auth_env)
      secrets     = concat(local.common_secrets, local.auth_secrets)

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "server"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 10
      }
    },
  ])
}

resource "aws_ecs_service" "server" {
  name            = "${var.name_prefix}-server"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.server.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  tags            = local.common_tags

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.server.arn
    container_name   = "server"
    container_port   = 8080
  }

  depends_on = [aws_lb_target_group.server]
}

# ---------- Bridge Task Definition + Service ----------

resource "aws_ecs_task_definition" "bridge" {
  family                   = "${var.name_prefix}-bridge"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.bridge_cpu
  memory                   = var.bridge_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  tags                     = local.common_tags

  container_definitions = jsonencode([
    {
      name      = "bridge"
      image     = local.full_image
      essential = true
      command   = ["turnstone-bridge"]

      environment = concat(local.common_env, local.auth_env)
      secrets     = concat(local.common_secrets, local.auth_secrets)

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "bridge"
        }
      }
    },
  ])
}

resource "aws_ecs_service" "bridge" {
  name            = "${var.name_prefix}-bridge"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.bridge.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  tags            = local.common_tags

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  depends_on = [aws_ecs_service.server]
}

# ---------- Console Task Definition + Service ----------

resource "aws_ecs_task_definition" "console" {
  family                   = "${var.name_prefix}-console"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.console_cpu
  memory                   = var.console_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  tags                     = local.common_tags

  container_definitions = jsonencode([
    {
      name      = "console"
      image     = local.full_image
      essential = true
      command   = ["turnstone-console", "--host", "0.0.0.0", "--port", "8090"]

      portMappings = [
        { containerPort = 8090, protocol = "tcp" },
      ]

      environment = concat(local.common_env, local.auth_env)
      secrets     = concat(local.common_secrets, local.auth_secrets)

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "console"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8090/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 10
      }
    },
  ])
}

resource "aws_ecs_service" "console" {
  name            = "${var.name_prefix}-console"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.console.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  tags            = local.common_tags

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.console.arn
    container_name   = "console"
    container_port   = 8090
  }

  depends_on = [aws_lb_target_group.console]
}

# ---------- Data Sources ----------

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
