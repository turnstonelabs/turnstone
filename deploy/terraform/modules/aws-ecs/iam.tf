# ---------- ECS Task Execution Role ----------
# Used by the ECS agent to pull images and retrieve secrets.

resource "aws_iam_role" "ecs_execution" {
  name = "${var.name_prefix}-ecs-execution-${var.environment}"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_base" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "${var.name_prefix}-secrets-read-${var.environment}"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = concat(
          [
            aws_secretsmanager_secret.openai_api_key.arn,
            aws_secretsmanager_secret.db_password.arn,
            aws_secretsmanager_secret.jwt_secret.arn,
          ],
        )
      },
    ]
  })
}

# ---------- ECS Task Role ----------
# Assumed by the running container. Minimal permissions; extend as needed.

resource "aws_iam_role" "ecs_task" {
  name = "${var.name_prefix}-ecs-task-${var.environment}"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      },
    ]
  })
}
