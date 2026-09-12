//
// SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
// SPDX-License-Identifier: GPL-3.0-or-later
//

protocol SystemMessageTableViewCellDelegate: AnyObject {
    func cellWantsToCollapseMessages(with message: NCChatMessage)
}


