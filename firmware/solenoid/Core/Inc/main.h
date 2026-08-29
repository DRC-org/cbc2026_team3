/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.h
  * @brief          : Header for main.c file.
  *                   This file contains the common defines of the application.
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

/* Includes ------------------------------------------------------------------*/
#include "stm32f3xx_hal.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Exported types ------------------------------------------------------------*/
/* USER CODE BEGIN ET */

/* USER CODE END ET */

/* Exported constants --------------------------------------------------------*/
/* USER CODE BEGIN EC */

/* USER CODE END EC */

/* Exported macro ------------------------------------------------------------*/
/* USER CODE BEGIN EM */

/* USER CODE END EM */

/* Exported functions prototypes ---------------------------------------------*/
void Error_Handler(void);

/* USER CODE BEGIN EFP */

/* USER CODE END EFP */

/* Private defines -----------------------------------------------------------*/
#define LED_BI_Pin GPIO_PIN_5
#define LED_BI_GPIO_Port GPIOA
#define DIP4_Pin GPIO_PIN_6
#define DIP4_GPIO_Port GPIOA
#define DIP3_Pin GPIO_PIN_7
#define DIP3_GPIO_Port GPIOA
#define DIP2_Pin GPIO_PIN_0
#define DIP2_GPIO_Port GPIOB
#define DIP1_Pin GPIO_PIN_1
#define DIP1_GPIO_Port GPIOB
#define PUMP6_SW_Pin GPIO_PIN_15
#define PUMP6_SW_GPIO_Port GPIOA
#define PUMP5_SW_Pin GPIO_PIN_3
#define PUMP5_SW_GPIO_Port GPIOB
#define PUMP4_SW_Pin GPIO_PIN_4
#define PUMP4_SW_GPIO_Port GPIOB
#define PUMP3_SW_Pin GPIO_PIN_5
#define PUMP3_SW_GPIO_Port GPIOB
#define PUMP2_SW_Pin GPIO_PIN_6
#define PUMP2_SW_GPIO_Port GPIOB
#define PUMP1_SW_Pin GPIO_PIN_7
#define PUMP1_SW_GPIO_Port GPIOB

/* USER CODE BEGIN Private defines */

/* USER CODE END Private defines */

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
