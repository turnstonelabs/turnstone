# ---------- ALB Security Group ----------

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb-${var.environment}"
  description = "Allow inbound HTTP to ALB for server and console"
  vpc_id      = var.vpc_id
  tags        = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP traffic to server"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count             = var.certificate_arn != "" ? 1 : 0
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS traffic to server"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_console" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP traffic to console"
  from_port         = 8090
  to_port           = 8090
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_console_https" {
  count             = var.certificate_arn != "" ? 1 : 0
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS traffic to console"
  from_port         = 8443
  to_port           = 8443
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.common_tags
}

resource "aws_vpc_security_group_egress_rule" "alb_all" {
  security_group_id = aws_security_group.alb.id
  description       = "Allow all outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.common_tags
}

# ---------- ECS Tasks Security Group ----------

resource "aws_security_group" "ecs_tasks" {
  name        = "${var.name_prefix}-ecs-tasks-${var.environment}"
  description = "Allow traffic from ALB to ECS tasks and outbound internet"
  vpc_id      = var.vpc_id
  tags        = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "ecs_from_alb_server" {
  security_group_id            = aws_security_group.ecs_tasks.id
  description                  = "Server port from ALB"
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.alb.id
  tags                         = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "ecs_from_alb_console" {
  security_group_id            = aws_security_group.ecs_tasks.id
  description                  = "Console port from ALB"
  from_port                    = 8090
  to_port                      = 8090
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.alb.id
  tags                         = local.common_tags
}

resource "aws_vpc_security_group_egress_rule" "ecs_all" {
  security_group_id = aws_security_group.ecs_tasks.id
  description       = "Allow all outbound (LLM APIs, ECR, Secrets Manager, etc.)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.common_tags
}

# ---------- RDS Security Group ----------

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds-${var.environment}"
  description = "Allow PostgreSQL access from ECS tasks"
  vpc_id      = var.vpc_id
  tags        = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_ecs" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL from ECS tasks"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  tags                         = local.common_tags
}

# ---------- Redis Security Group ----------

resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis-${var.environment}"
  description = "Allow Redis access from ECS tasks"
  vpc_id      = var.vpc_id
  tags        = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_ecs" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from ECS tasks"
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  tags                         = local.common_tags
}
